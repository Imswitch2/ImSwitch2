import torch
import torch.nn as nn
import warnings


def swish(x):
    return x*torch.sigmoid(x)

def getDivisors(n) : 
    divisors = []
    i = 1
    while i <= n : 
        if (n % i==0) : 
            divisors.append(i), 
        i = i + 1
    return divisors


def find_closest_divisor(a,b):
    if a % b == 0:
        return b
    all  = getDivisors(a)
    for idx,val in enumerate(all):
        if b < val:
            if idx == 0:
                return b
            if (val-b)>(b-all[idx-1]):
                return all[idx-1]
            return val
        
def normalize(norm_type,channels,groups):
    if norm_type == 'group':
        if channels % groups != 0:
            groups = find_closest_divisor(channels,groups)
        norm = torch.nn.GroupNorm(groups, channels)
    elif norm_type == 'batch':
        norm = torch.nn.BatchNorm2d(channels)
    else:
        warnings.warn ("Normalization type unknown -> set to None.")
        norm = None

    return norm


class downsamp_block(nn.Module):
    """
    Downsamples with strided convolution.
    """
    def __init__(self,in_channels,out_channels,activation = swish,kernel_size=2,
                 padding=0,stride = 2,norm = 'group',gr_norm = 8,dropout=0.0):
        super().__init__()
        self.conv=nn.Conv2d(in_channels,out_channels,kernel_size=kernel_size,padding=padding,stride = stride)
        self.activation = activation
        if norm is not None:
            self.norm = normalize(norm,out_channels,gr_norm)
        else: 
            self.norm = None
        self.dropout = torch.nn.Dropout(dropout)

    def forward(self, x):
        x = self.conv(x)
        x = self.activation(x)
        x = self.dropout(x)
        if self.norm is not None:
            x = self.norm(x)
        return x


class upsamp_block(nn.Module):
    """
    Upsamples with transposed convolution.
    """
    def __init__(self,in_channels,out_channels,activation = swish,kernel_size=2,
                 padding=0,stride = 2,norm = 'group',gr_norm = 8, dropout = 0.0):
        super().__init__()
        self.conv=nn.ConvTranspose2d(in_channels,out_channels,kernel_size=kernel_size,padding=padding,stride = stride)
        self.activation = activation
        if norm is not None:
            self.norm = normalize(norm,out_channels,gr_norm)
        else: 
            self.norm = None
        self.dropout = torch.nn.Dropout(dropout)

    def forward(self, x):
        x = self.conv(x)
        x = self.activation(x)
        x = self.dropout(x)
        if self.norm is not None:
            x = self.norm(x)
        return x
