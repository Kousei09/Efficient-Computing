# 2023.11-Modified some parts in the code
#            Huawei Technologies Co., Ltd. <foss@huawei.com>
from .bottleneck_attn import BottleneckAttn
from .halo_attn import HaloAttn
from .lambda_layer import LambdaLayer
from .swin_attn import WindowAttention


def get_self_attn(attn_type):
    if attn_type == 'bottleneck':
        return BottleneckAttn
    elif attn_type == 'halo':
        return HaloAttn
    elif attn_type == 'lambda':
        return LambdaLayer
    elif attn_type == 'swin':
        return WindowAttention
    else:
        assert False, f"Unknown attn type ({attn_type})"


def create_self_attn(attn_type, dim, stride=1, **kwargs):
    attn_fn = get_self_attn(attn_type)
    return attn_fn(dim, stride=stride, **kwargs)
