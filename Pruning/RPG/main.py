# 2023.11-Modified some parts in the code
#            Huawei Technologies Co., Ltd. <foss@huawei.com>

# Copyright (c) 2018-2019, NVIDIA CORPORATION
# Copyright (c) 2017-      Facebook, Inc
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
import argparse
import os
import shutil
import time
import random
from datetime import datetime
# os.environ['CUDA_VISIBLE_DEVICES']='7'
import copy
import numpy as np
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets

try:
    from apex.parallel import DistributedDataParallel as DDP
    from apex.fp16_utils import *
    from apex import amp
except ImportError:
    raise ImportError(
        "Please install apex from https://www.github.com/nvidia/apex to run this example."
    )

import image_classification.resnet as models
import image_classification.logger as log

from image_classification.smoothing import LabelSmoothing
from image_classification.mixup import NLLMultiLabelSmooth, MixUpWrapper
from image_classification.dataloaders import *
from image_classification.training import *
from image_classification.utils import *
from image_classification.resnet import add_netarch_parser_arguments
#from image_classification.mobilenetv2 import add_mobilenet_parser_arguments

import time

import dllogger
import json

def load_lr(args,config_name):
    print(f'Loading JSON file: lrconfig/{config_name}.json')
    with open(f'lrconfig/{config_name}.json',) as f:
        lrconfig=json.load(f)
        args.lr=lrconfig['lr']
        args.lr_decay_epochs=lrconfig['decay_epochs']

        args.epochs=lrconfig['epochs']
        args.lr_schedule=lrconfig['scheduler']
        args.iterative_T_end_percent = lrconfig['iterative_T_end_percent']

torch.manual_seed(0)

def add_parser_arguments(parser):
    #model_names = models.resnet_versions.keys()
    #model_configs = models.resnet_configs.keys()

    parser.add_argument("--data", default="../data/imagenet", help="path to dataset")
    parser.add_argument(
        "--data-backend",
        metavar="BACKEND",
        default="dali-cpu",
        choices=DATA_BACKEND_CHOICES,
        help="data backend: "
        + " | ".join(DATA_BACKEND_CHOICES)
        + " (default: dali-cpu)",
    )

    parser.add_argument(
        "--arch",
        "-a",
        metavar="ARCH",
        default="resnet50",
        help="model architecture: (default: resnet50), or mobilenetv2",
    )

    parser.add_argument(
        "--model-config",
        "-c",
        metavar="CONF",
        default="classic",
        #choices=model_configs,
        help="model configs: (default: classic)",
    )

    parser.add_argument(
        "--num-classes",
        metavar="N",
        default=1000,
        type=int,
        help="number of classes in the dataset",
    )

    parser.add_argument(
        "-j",
        "--workers",
        default=8,
        type=int,
        metavar="N",
        help="number of data loading workers (default: 8)",
    )
    parser.add_argument(
        "--epochs",
        default=90,
        type=int,
        metavar="N",
        help="number of total epochs to run",
    )
    parser.add_argument(
        "--run-epochs",
        default=-1,
        type=int,
        metavar="N",
        help="run only N epochs, used for checkpointing runs",
    )
    parser.add_argument(
        "-b",
        "--batch-size",
        default=64,
        type=int,
        metavar="N",
        help="mini-batch size (default: 256) per gpu",
    )

    parser.add_argument(
        "--optimizer-batch-size",
        default=-1,
        type=int,
        metavar="N",
        help="size of a total batch size, for simulating bigger batches using gradient accumulation",
    )

    parser.add_argument(
        "--lr",
        "--learning-rate",
        default=0.1,
        type=float,
        metavar="LR",
        help="initial learning rate",
    )
    parser.add_argument(
        "--lr-schedule",
        default="step",
        type=str,
        metavar="SCHEDULE",
        choices=["step", "linear", "cosine", "cosine2", "repeat_cosine", "manual"],
        help="Type of LR schedule: {}, {}, {}, {}, {}, {}".format("step", "linear", "cosine", "cosine2", "repeat_cosine", "manual"),
    )

    parser.add_argument(
        "--warmup", default=0, type=int, metavar="E", help="number of warmup epochs"
    )

    parser.add_argument(
        "--label-smoothing",
        default=0.0,
        type=float,
        metavar="S",
        help="label smoothing",
    )
    parser.add_argument(
        "--mixup", default=0.0, type=float, metavar="ALPHA", help="mixup alpha"
    )

    parser.add_argument('--optimizer', default='sgd', type=str, metavar='OPTIMIZER',
                        help='Optimizer (default: "sgd"')

    parser.add_argument(
        "--momentum", default=0.9, type=float, metavar="M", help="momentum"
    )
    parser.add_argument(
        "--weight-decay",
        "--wd",
        default=1e-4,
        type=float,
        metavar="W",
        help="weight decay (default: 1e-4)",
    )
    parser.add_argument(
        "--bn-weight-decay",
        action="store_true",
        help="use weight_decay on batch normalization learnable parameters, (default: false)",
    )
    parser.add_argument(
        "--nesterov",
        action="store_true",
        help="use nesterov momentum, (default: false)",
    )

    parser.add_argument(
        "--print-freq",
        "-p",
        default=10,
        type=int,
        metavar="N",
        help="print frequency (default: 10)",
    )
    parser.add_argument(
        "--resume",
        default=None,
        type=str,
        metavar="PATH",
        help="path to latest checkpoint (default: none)",
    )
    parser.add_argument(
        "--pretrained-weights",
        default="",
        type=str,
        metavar="PATH",
        help="load weights from here",
    )

    parser.add_argument("--fp16", action="store_true", help="Run model fp16 mode.")
    parser.add_argument(
        "--static-loss-scale",
        type=float,
        default=1,
        help="Static loss scale, positive power of 2 values can improve fp16 convergence.",
    )
    parser.add_argument(
        "--dynamic-loss-scale",
        action="store_true",
        help="Use dynamic loss scaling.  If supplied, this argument supersedes "
        + "--static-loss-scale.",
    )
    parser.add_argument(
        "--prof", type=int, default=-1, metavar="N", help="Run only N iterations"
    )
    parser.add_argument(
        "--amp",
        action="store_true",
        help="Run model AMP (automatic mixed precision) mode.",
    )

    parser.add_argument(
        "--seed", default=None, type=int, help="random seed used for numpy and pytorch"
    )

    parser.add_argument(
        "--gather-checkpoints",
        action="store_true",
        help="Gather checkpoints throughout the training, without this flag only best and last checkpoints will be stored",
    )

    parser.add_argument(
        "--raport-file",
        default="experiment_raport.json",
        type=str,
        help="file in which to store JSON experiment raport",
    )

    parser.add_argument(
        "--evaluate", action="store_true", help="evaluate checkpoint/model"
    )
    parser.add_argument("--training-only", action="store_true", help="do not evaluate")

    parser.add_argument(
        "--no-checkpoints",
        action="store_false",
        dest="save_checkpoints",
        help="do not store any checkpoints, useful for benchmarking",
    )

    parser.add_argument("--checkpoint-filename", default="checkpoint.pth.tar", type=str)
    parser.add_argument("--checkpoint-dir", default=None, type=str, help='dir to save checkpoints, will override self naming')
    parser.add_argument("--log-filename", default='./temp_log', type=str, help='log filename, will override self naming')


    parser.add_argument(
        "--workspace",
        type=str,
        default="./",
        metavar="DIR",
        help="path to directory where checkpoints will be stored",
    )
    parser.add_argument(
        "--memory-format",
        type=str,
        default="nchw",
        choices=["nchw", "nhwc"],
        help="memory layout, nchw or nhwc",
    )

    parser.add_argument('--id', default='', type=str, help='traing id for different run')
    parser.add_argument('--lr-decay-epochs', default='30-60-80',type=str,help='weight decay at these epochs')
    parser.add_argument("--short-train",action="store_true",dest="short_train",help="debug training, only 200 minibatches per epoch")
    parser.add_argument("--do-not-reset-epochs", action="store_true", help="continue training, do not reset epoch to be 0")
    parser.add_argument("--restart-training", action="store_true", help="restart training from epoch 0")
    parser.add_argument('--lr-file', default=None, type=str, help='manual learning file')

    # rigl settings
    parser.add_argument('--dense-allocation', default=None, type=float,
                        help='percentage of dense parameters allowed. if None, pruning will not be used. must be on the interval (0, 1]')
    parser.add_argument('--delta', default=100, type=int,
                        help='delta param for pruning')
    parser.add_argument('--grad-accumulation-n', default=1, type=int,
                        help='number of gradients to accumulate before scoring for rigl')
    parser.add_argument('--alpha', default=0.3, type=float,
                        help='alpha param for pruning')
    parser.add_argument('--static-topo', default=0, type=int,
                        help='if 1, use random sparsity topo and remain static')
    parser.add_argument('--T-end-percent', default=0.8, type=float,
                        help='percentage of total samples to stop rigl topo updates')
    parser.add_argument('--T-end-epochs', default=None, type=float,
                        help='number of epochs to simulate (only used for tuning)')
    parser.add_argument('--sp-distribution', default="uniform", type=str,
                        help='sparsity distribution, (erk or uniform)')
    # extra added
    parser.add_argument('--dataset', default="imagenet", type=str)
    parser.add_argument('--eval-batch-size', default=1024, type=int)
    parser.add_argument('--lrconfig', type=str,default=None, help='lr config JSON file')
    # rank loss parsers
    parser.add_argument('--lamb', default=0.0, type=float)
    parser.add_argument('--sparsity_thres', default=0.0, type=float)
    parser.add_argument('--partial_k', type=float,default=0.3, help='partial rank constraint')
    parser.add_argument('--bounded', action="store_true", help='Whether rank loss is bounded')
    # iterative
    parser.add_argument('--iterative_T_end_percent', type=float,default=0.0, help='sparsity distribution')


def main(args):
    exp_start_time = time.time()
    global best_prec1
    best_prec1 = 0

    args.distributed = False
    if "WORLD_SIZE" in os.environ:
        print("=======SETTED========")
        args.distributed = int(os.environ["WORLD_SIZE"]) > 1
        args.local_rank = int(os.environ["LOCAL_RANK"])
    if args.distributed:
        print('---DISTRIBUTED MODE ACTIVATED---')
    args.gpu = 0
    args.world_size = 1

    if args.distributed:
        args.gpu = args.local_rank % torch.cuda.device_count()
        torch.cuda.set_device(args.gpu)
        dist.init_process_group(backend="nccl", init_method="env://")
        args.world_size = torch.distributed.get_world_size()

    if args.amp and args.fp16:
        print("Please use only one of the --fp16/--amp flags")
        exit(1)

    if args.seed is not None:
        print("Using seed = {}".format(args.seed))
        if not hasattr(args,'local_rank'):
            args.local_rank = 0
        torch.manual_seed(args.seed + args.local_rank)
        torch.cuda.manual_seed(args.seed + args.local_rank)
        np.random.seed(seed=args.seed + args.local_rank)
        random.seed(args.seed + args.local_rank)

        def _worker_init_fn(id):
            np.random.seed(seed=args.seed + args.local_rank + id)
            random.seed(args.seed + args.local_rank + id)

    else:

        def _worker_init_fn(id):
            pass

    if args.fp16:
        assert (
            torch.backends.cudnn.enabled
        ), "fp16 mode requires cudnn backend to be enabled."

    if args.static_loss_scale != 1.0:
        if not args.fp16:
            print("Warning:  if --fp16 is not used, static_loss_scale will be ignored.")

    if args.optimizer_batch_size < 0:
        batch_size_multiplier = 1
    else:
        tbs = args.world_size * args.batch_size
        if args.optimizer_batch_size % tbs != 0:
            print(
                "Warning: simulated batch size {} is not divisible by actual batch size {}".format(
                    args.optimizer_batch_size, tbs
                )
            )
        batch_size_multiplier = int(args.optimizer_batch_size / tbs)
        print("BSM: {}".format(batch_size_multiplier))

    pretrained_weights = None
    if args.pretrained_weights and 'ViT' not in args.arch:
        if os.path.isfile(args.pretrained_weights):
            print(
                "=> loading pretrained weights from '{}'".format(
                    args.pretrained_weights
                )
            )
            pretrained_weights = torch.load(args.pretrained_weights)
        else:
            print("=> no pretrained weights found at '{}'".format(args.resume))



    start_epoch = 0
    pruner_state_dict = None
    # optionally resume from a checkpoint
    if args.resume is not None:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(
                args.resume, map_location=lambda storage, loc: storage.cuda(args.gpu)
            )
            if args.restart_training:
                start_epoch = 0
                best_prec1 = 0
                optimizer_state = None
                model_state = checkpoint["state_dict"]
                print("Restart training")

            elif "epoch" in checkpoint and "best_prec1" in checkpoint and "optimizer" in checkpoint and "pruner" in checkpoint:
                start_epoch = checkpoint["epoch"]
                best_prec1 = checkpoint["best_prec1"]
                optimizer_state = checkpoint["optimizer"]
                pruner_state_dict = checkpoint["pruner"]
                model_state = checkpoint["state_dict"]
            else:
                model_state = checkpoint
                start_epoch = 0
                best_prec1 = 0
                optimizer_state = None

            if "epoch" in checkpoint:
                print("=> loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint["epoch"]))
            else:
                print("=> loaded checkpoint '{}' ".format(args.resume))

        else:
            print("=> no checkpoint found at '{}'".format(args.resume))
            time.sleep(10)
            # exit()
            model_state = None
            optimizer_state = None
    else:
        model_state = None
        optimizer_state = None

    loss = nn.CrossEntropyLoss
    if args.mixup > 0.0:
        loss = lambda: NLLMultiLabelSmooth(args.label_smoothing)
    elif args.label_smoothing > 0.0:
        loss = lambda: LabelSmoothing(args.label_smoothing)

    memory_format = (
        torch.channels_last if args.memory_format == "nhwc" else torch.contiguous_format
    )

    model_and_loss = ModelAndLoss(
        (args.arch, args.model_config, args.num_classes),
        loss,
        pretrained_weights=pretrained_weights,
        cuda=True,
        fp16=args.fp16,
        memory_format=memory_format,
        args=args,
    )

    for name, W in model_and_loss.model.named_parameters():
        if 'weight' in name and 'bn' not in name:
            print(name, W.shape)
    #print(model_and_loss)
    if 'cifar' in args.dataset:
        from get_datasets.get_cifar import get_cifar
        train_loader, val_loader = get_cifar(args, start_epoch)
        train_loader_len, val_loader_len = len(train_loader), len(val_loader)

    elif args.dataset == "imagenet":
        # Create data loaders and optimizers as needed
        if args.data_backend == "pytorch":
            get_train_loader = get_pytorch_train_loader
            get_val_loader = get_pytorch_val_loader
        elif args.data_backend == "dali-gpu":
            get_train_loader = get_dali_train_loader(dali_cpu=False)
            get_val_loader = get_dali_val_loader()
        elif args.data_backend == "dali-cpu":
            get_train_loader = get_dali_train_loader(dali_cpu=True)
            get_val_loader = get_dali_val_loader()
        elif args.data_backend == "syntetic":
            get_val_loader = get_syntetic_loader
            get_train_loader = get_syntetic_loader

        train_loader, train_loader_len = get_train_loader(
            args.data,
            args.batch_size,
            args.num_classes,
            args.mixup > 0.0,
            start_epoch=start_epoch,
            workers=args.workers,
            fp16=args.fp16,
            memory_format=memory_format,
        )
        if args.mixup != 0.0:
            train_loader = MixUpWrapper(args.mixup, train_loader)

        val_loader, val_loader_len = get_val_loader(
            args.data,
            args.batch_size,
            args.num_classes,
            False,
            workers=args.workers,
            fp16=args.fp16,
            memory_format=memory_format,
        )

    if not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0:
        logger = log.Logger(
            args.print_freq,
            [
                dllogger.StdOutBackend(
                    dllogger.Verbosity.DEFAULT, step_format=log.format_step
                ),
                dllogger.JSONStreamBackend(
                    dllogger.Verbosity.VERBOSE,
                    os.path.join(args.workspace, args.raport_file),
                ),
            ],
            start_epoch=start_epoch - 1,
        )

    else:
        logger = log.Logger(args.print_freq, [], start_epoch=start_epoch - 1)

    logger.log_parameter(args.__dict__, verbosity=dllogger.Verbosity.DEFAULT)


    optimizer = get_optimizer(
        list(model_and_loss.model.named_parameters()),
        args.fp16,
        args.lr,
        args.momentum,
        args.weight_decay,
        nesterov=args.nesterov,
        bn_weight_decay=args.bn_weight_decay,
        state=optimizer_state,
        static_loss_scale=args.static_loss_scale,
        dynamic_loss_scale=args.dynamic_loss_scale,
        optimizer_type=args.optimizer,
    )

    if args.lr_schedule == "step":
        lr_decay_epochs = args.lr_decay_epochs.split('-')
        for i in range(len(lr_decay_epochs)):
            lr_decay_epochs[i] = int(lr_decay_epochs[i])

        lr_policy = lr_step_policy(
            args.lr, lr_decay_epochs, 0.1, args.warmup, logger=logger
        )
    elif args.lr_schedule == "cosine":
        lr_policy = lr_cosine_policy(args.lr, args.warmup, args.epochs, logger=logger)
    elif args.lr_schedule == "linear":
        lr_policy = lr_linear_policy(args.lr, args.warmup, args.epochs, logger=logger)
    elif args.lr_schedule == "exponential":
        lr_policy = lr_exponential_policy(args.lr, args.warmup, args.epochs, final_multiplier=0.001, logger=logger)
    elif args.lr_schedule == "repeat_cosine":
        assert args.sp_mask_update_freq is not None
        lr_policy = lr_repeat_cosine_policy(args.lr, args.warmup, args.epochs, args.sp_mask_update_freq, train_loader_len, logger=logger)
    elif args.lr_schedule == "cosine2":
        lr_policy = lr_cosine2_policy(args.lr, args.warmup, args.epochs, train_loader_len, logger=logger)
    elif args.lr_schedule == "manual":
        lr_policy = lr_manual_policy(args.lr_file, args.epochs, logger=logger)

    if args.amp:
        model_and_loss, optimizer = amp.initialize(
            model_and_loss,
            optimizer,
            opt_level="O1",
            loss_scale="dynamic" if args.dynamic_loss_scale else args.static_loss_scale,
        )

    if args.distributed:
        model_and_loss.distributed()
        print("========MODEL AND LOSS DISTRIBUTED=========")

    if args.evaluate:
        model_state_1 = {}
        keep_prefix = False
        for key, W in model_and_loss.model.named_parameters():
            if key.startswith('module.'):
                keep_prefix = True
        if keep_prefix:
            pass
        else:
            for key in model_state:
                #print(key)
                if key.startswith('module.'):
                    new_key = key[7:]
                    model_state_1[new_key] = model_state[key]
            model_state = copy.copy(model_state_1)


    model_and_loss.load_model_state(model_state)

    checkpoint_dir = args.checkpoint_dir
    log_filename = args.log_filename
    print(f"======Checkpoint Dir: {checkpoint_dir}=========")
    print(log_filename)


    if not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0:

        if not args.evaluate and args.save_checkpoints:

            if os.path.isdir(checkpoint_dir) is False:
                os.system('mkdir -p ' + checkpoint_dir)
                print("New folder {} created...".format(checkpoint_dir))

            log_filename_dir_str = log_filename.split('/')
            log_filename_dir = "/".join(log_filename_dir_str[:-1])
            if not os.path.exists(log_filename_dir):
                os.system('mkdir -p ' + log_filename_dir)
                print("New folder {} created...".format(log_filename_dir))

            with open(log_filename, 'a') as f:
                for arg in sorted(vars(args)):
                    f.write("{}:".format(arg))
                    f.write("{}".format(getattr(args, arg)))
                    f.write("\n")

            print(checkpoint_dir)
            print(log_filename)



    time.sleep(1)

    ## insert total time counter & flops counter
    args.rank_loss_calc_time = []
    args.total_pruning_time = time.time()
    args.svd_flops = {
        'min_solve': 0,
        'min_iter':0,
        'solve': 0,
        'iter': 0,
    }

    train_loop(
        model_and_loss,
        optimizer,
        lr_policy,
        train_loader,
        val_loader,
        args.fp16,
        logger,
        should_backup_checkpoint(args),
        use_amp=args.amp,
        batch_size_multiplier=batch_size_multiplier,
        start_epoch=start_epoch,
        end_epoch=(start_epoch + args.run_epochs)
        if args.run_epochs != -1
        else args.epochs,
        best_prec1=best_prec1,
        prof=args.prof,
        skip_training=args.evaluate,
        skip_validation=args.training_only,
        save_checkpoints=args.save_checkpoints and not args.evaluate,
        checkpoint_dir=checkpoint_dir,
        checkpoint_filename=args.checkpoint_filename,
        log_file=log_filename,
        args=args,
        pruner_state_dict=pruner_state_dict,
        train_loader_len=train_loader_len
    )
    # timer_stop
    args.total_pruning_time = time.time() - args.total_pruning_time

    exp_duration = time.time() - exp_start_time
    if not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0:
        logger.end()
    print("Experiment ended")

    ## total_time_output
    if not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0:
        print(f'Total pruning time: {args.total_pruning_time:.2f} Sec.')
        if hasattr(args, 'rank_loss_calc_time') and args.rank_loss_calc_time != []:
            temp_sum = sum(args.rank_loss_calc_time)
            print(f'Sum rank loss calc time: {temp_sum:.2f} Sec.')
            print(f'Avg rank loss calc time: {(temp_sum/len(args.rank_loss_calc_time)):.5f} Sec.')
        print(f'SVD FLOPs info: {args.svd_flops}')

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PyTorch ImageNet Training")

    add_parser_arguments(parser)
    add_netarch_parser_arguments(parser)
    args = parser.parse_args()
    if args.lrconfig is not None:
        load_lr(args, args.lrconfig)
    cudnn.benchmark = True

    if len(args.id) == 0:
        now = datetime.now()
        now = str(now)
        now = now.replace(" ","_")
        now = now.replace(":","_")
        now = now.replace("-","_")
        now = now.replace(".","_")
        args.id = now


    print(args)

    main(args)
