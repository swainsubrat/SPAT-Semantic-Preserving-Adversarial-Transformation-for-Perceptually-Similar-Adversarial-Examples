"""
Pytorch-Lightning implementation of the vanilla autoencoder
"""
import sys

sys.path.append("..")

from typing import Any, Callable

import matplotlib.pyplot as plt
import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn.functional as F
import torchvision
import torchvision.transforms as transforms
from pl_bolts.models.autoencoders import AE
from pl_bolts.models.autoencoders.components import (resnet18_decoder,
                                                     resnet18_encoder)
from sklearn.manifold import TSNE
from torch import nn
from torchmetrics.functional import accuracy
from torchviz import make_dot

import warnings

warnings.filterwarnings("ignore", category=UserWarning, message="The feature warn_missing_pkg is currently marked under review.")

device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

from dataloader import (load_celeba, load_cifar, load_fashion_mnist,
                        load_imagenet_x, load_mnist, load_gtsrb)
from models.classifier import CIFAR10Classifier
from utils import visualize_cifar_reconstructions

from .vgg_imagenet import VGGDecoder, VGGEncoder, get_configs


def double_conv(in_channels, out_channels):
    return nn.Sequential(
        nn.Conv2d(in_channels, out_channels, kernel_size = 3, padding=1),
        nn.BatchNorm2d(out_channels),
        nn.ReLU(inplace=True),
        # nn.Dropout(p=0.5), ## regualarisation..using high dropout rate of 0.9...lets see for few moments...
        nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1),
        nn.BatchNorm2d(out_channels),
        nn.ReLU(inplace=True),
        # nn.Dropout(p=0.9) ## dual dropout 
    )


def down(in_channels, out_channels):
    ## downsampling with maxpool then double conv
    return nn.Sequential(
        nn.MaxPool2d(2),
        double_conv(in_channels, out_channels)
    )


def outconv(in_channels, out_channels):
    return nn.Conv2d(in_channels, out_channels, kernel_size=1)


class up(nn.Module):
    ## upsampling then double conv 
    def __init__(self, in_channels, out_channels):
        super(up, self).__init__()
        self.up = nn.ConvTranspose2d(in_channels, in_channels, kernel_size=2, stride = 2)
        self.conv = double_conv(in_channels, out_channels)
    def forward(self, x1, x2): 
        x1 = self.up(x1)
        # input is CHW 
        diffY = x2.size()[2] - x1.size()[2]
        diffX = x2.size()[3] - x1.size()[3]

        x = F.pad(x1, [diffX // 2, diffX - diffX // 2,
                        diffY // 2, diffY - diffY // 2])
        return self.conv(x)


class MaskedLinear(nn.Linear):
    def __init__(self, *args, mask, **kwargs):
        super().__init__(*args, **kwargs)
        self.mask = mask
        self.sigmoid = nn.Sigmoid()
        self.b = torch.zeros(self.bias.shape).to(device)

    def forward(self, input):
        out = F.linear(input, self.mask, self.b)
        out = self.sigmoid(out)

        return out


class BaseAutoEncoder(pl.LightningModule):
    """
    Base class for all the autoencoders
    """
    def __init__(self):
        super().__init__()
        self.define_encoder()
        self.define_decoder()

    def define_encoder(self):
        """
        Define encoder by overriding this function
        """
        raise NotImplementedError

    def define_decoder(self):
        """
        Define decoder by overriding this function
        """
        raise NotImplementedError

    def get_z(self, x: torch.Tensor) -> torch.Tensor:
        """
        Get the embedding of the input

        Args:
            x (torch.Tensor): Input image to the encoder
        
        Returns:
            Encoded input
        """
        return self.encoder(x)

    def get_x_hat(self, z: torch.Tensor) -> torch.Tensor:
        """
        Get the reconstructed output

        Args:
            z (torch.Tensor): Image embeddings
        
        Returns:
            Reconstructed output
        """
        return self.decoder(z)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z = self.encoder(x)
        x_hat = self.decoder(z)

        return x_hat, z

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)


class ANNAutoencoder(BaseAutoEncoder):
    """
    This is an implementation of the autoencoder using ANN
    """
    def __init__(self,
                input_dim: int=784,
                latent_dim: int=128,
                activation_fn: nn.modules.activation=nn.ReLU) -> None:
        """
        Args:
            input_dim (int): Dimension of the input to the autoencoder
            latent_dim (int): Dimension of the latent dimension
            activation_fn (nn.modules.activation): Activation function 
        """
        self.save_hyperparameters()
        self.input_dim     = input_dim
        self.latent_dim    = latent_dim
        self.activation_fn = activation_fn
        super().__init__()

    def define_encoder(self) -> None:
        self.encoder = nn.Sequential(
            nn.Linear(self.input_dim, 512),
            self.activation_fn(),
            nn.Linear(512, 264),
            self.activation_fn(),
            nn.Linear(264, self.latent_dim),
            # self.activation_fn()
        )

    def define_decoder(self) -> None:
        self.decoder = nn.Sequential(
            nn.Linear(self.latent_dim, 264),
            self.activation_fn(),
            nn.Linear(264, 512),
            self.activation_fn(),
            nn.Linear(512, 784),
            nn.Sigmoid() # for making value between 0 to 1
        )

    def training_step(self, batch, batch_idx):
        x, y = batch
        x = x.view(x.size(0), -1)
        x_hat, _ = self(x)
        loss = F.mse_loss(x_hat, x, reduction="none").sum(dim=[1]).mean(dim=[0])
        self.log("train_loss", loss, on_step=True, on_epoch=True,\
                prog_bar=True, logger=True)

        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch
        x = x.view(x.size(0), -1)
        x_hat, _ = self(x)
        loss = F.mse_loss(x, x_hat, reduction="none").sum(dim=[1]).mean(dim=[0])
        self.log("valid_loss", loss)

    def predict_step(self, batch, batch_idx):
        x, y = batch

        return self(x)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.2, patience=20, min_lr=5e-5)
        
        return {"optimizer": optimizer, "lr_scheduler": scheduler, "monitor": "valid_loss"}


class MNISTCNNAutoencoder(BaseAutoEncoder):
    """
    This is an implementation of the autoencoder for MNIST
    """
    def __init__(self,
                latent_dim: int=32,
                base_channel_size: int=32,
                activation_fn: nn.modules.activation=nn.ReLU,
                perceptual_loss: bool=False,
                loss: Callable=None) -> None:
        """
        Args:
            latent_dim (int): Dimension of the latent dimension
            base_channel_size (int): Number of channels we use in the first 
            convolutional layers. Deeper layers might use a duplicate of it.
            activation_fn (nn.modules.activation): Activation function
            perceptual_loss (bool): Perceptual loss using LPIPS
            loss (Callable): Loss function for autoencoder
        """
        self.save_hyperparameters()
        self.latent_dim    = latent_dim
        self.c_hid         = base_channel_size
        self.activation_fn = activation_fn

        if perceptual_loss:
            if not loss:
                raise AttributeError("Pass a callable loss to the attribute \
                                      loss when perceptual loss is True")
        super().__init__()
        self.perceptual_loss = perceptual_loss
        self.loss            = loss
    
    def define_encoder(self) -> None:
        self.encoder = nn.Sequential(
            nn.Conv2d(1, 8, 3, stride=2, padding=1),
            nn.ReLU(True),
            nn.Conv2d(8, 16, 3, stride=2, padding=1),
            nn.BatchNorm2d(16),
            nn.ReLU(True),
            nn.Conv2d(16, 32, 3, stride=2, padding=0),
            nn.ReLU(True),
            nn.Flatten(start_dim=1),
            nn.Linear(3 * 3 * 32, 128),
            nn.ReLU(True),
            nn.Linear(128, self.latent_dim)
        )

    def define_decoder(self) -> None:
        self.decoder = nn.Sequential(
            nn.Linear(self.latent_dim, 128),
            nn.ReLU(True),
            nn.Linear(128, 3 * 3 * 32),
            nn.ReLU(True),
            nn.Unflatten(dim=1, 
                unflattened_size=(32, 3, 3)),
            nn.ConvTranspose2d(32, 16, 3, 
                stride=2, output_padding=0),
            nn.BatchNorm2d(16),
            nn.ReLU(True),
            nn.ConvTranspose2d(16, 8, 3, stride=2, 
                padding=1, output_padding=1),
            nn.BatchNorm2d(8),
            nn.ReLU(True),
            nn.ConvTranspose2d(8, 1, 3, stride=2, 
                padding=1, output_padding=1)
        )
    
    def get_z(self, x: torch.Tensor) -> torch.Tensor:
        """
        Get the embedding of the input

        Args:
            x (torch.Tensor): Input image to the encoder
        
        Returns:
            Encoded input
        """
        x = x.reshape((-1, 1, 28, 28))
        return self.encoder(x)

    # def get_x_hat(self, z: torch.Tensor) -> torch.Tensor:
    #     """
    #     Get the reconstructed output

    #     Args:
    #         z (torch.Tensor): Image embeddings
        
    #     Returns:
    #         Reconstructed output
    #     """
    #     return self.decoder(z)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.reshape((-1, 1, 28, 28))
        z = self.encoder(x)
        x_hat = self.decoder(z)

        return x_hat, z

    def training_step(self, batch, batch_idx):
        x, _ = batch
        x = x.reshape((-1, 1, 28, 28))
        x_hat, _ = self(x)
        if self.perceptual_loss:
            loss = self.loss(x, x_hat)
        else:
            loss = F.mse_loss(x, x_hat, reduction="none")
        loss = loss.sum(dim=[1, 2, 3]).mean(dim=[0])
        self.log("train_loss", loss)

        return loss
    
    def validation_step(self, batch, batch_idx):
        x, _ = batch
        x = x.reshape((-1, 1, 28, 28))
        x_hat, _ = self(x)
        if self.perceptual_loss:
            loss = self.loss(x, x_hat)
        else:
            loss = F.mse_loss(x, x_hat, reduction="none")
        loss = loss.sum(dim=[1, 2, 3]).mean(dim=[0])
        self.log("valid_loss", loss)
    
    def predict_step(self, batch, batch_idx):
        x, _ = batch
        
        return self(x)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.2, patience=20, min_lr=5e-5)
        
        return {"optimizer": optimizer, "lr_scheduler": scheduler, "monitor": "valid_loss"}


class ClassConstrainedANNAutoencoder(ANNAutoencoder):
    """
    This is an extension of ANN autoencoder with a class-constrain
    on the latent dimension
    """
    def __init__(self,
                input_dim: int=784,
                latent_dim: int=100,
                n_classes: int=10,
                activation_fn: nn.modules.activation=nn.ReLU) -> None:
        """
        Args:
            input_dim (int): Dimension of the input to the autoencoder
            latent_dim (int): Dimension of the latent dimension
            n_classes (int): Number of classes
            activation_fn (nn.modules.activation): Activation function 
        """
        self.save_hyperparameters()
        super().__init__(input_dim, latent_dim, activation_fn)

        assert latent_dim % n_classes == 0, "latent dimension should be divisible by number of classes"

        self.mask = torch.block_diag(*[torch.ones(1, latent_dim // n_classes),] * n_classes).to(device)
        self.linear = MaskedLinear(latent_dim, n_classes, mask=self.mask).to(device)
        # self.recon_loss = nn.BCELoss()
        # self.class_loss = nn.CrossEntropyLoss()

    def define_encoder(self) -> None:
        self.encoder = nn.Sequential(
            nn.Linear(self.input_dim, 512),
            self.activation_fn(),
            nn.Linear(512, 256),
            self.activation_fn(),
            nn.Linear(256, self.latent_dim)
        )

    def define_decoder(self) -> None:
        self.decoder = nn.Sequential(
            nn.Linear(self.latent_dim, 256),
            self.activation_fn(),
            nn.Linear(256, 512),
            self.activation_fn(),
            nn.Linear(512, 784),
            nn.Sigmoid() # for making value between 0 to 1
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z = self.encoder(x)
        
        # dummy = torch.Tensor(([1] * 10) + ([0] * 90)).reshape(-1, 100).to(device)
        preds = self.linear(z)

        x_hat = self.decoder(z)

        return x_hat, z, preds

    def training_step(self, batch, batch_idx):
        x, y = batch
        x = x.view(x.size(0), -1)
        x_hat, _, preds = self(x)
        class_loss = F.cross_entropy(preds, y)
        recon_loss = F.mse_loss(x, x_hat)
        loss = class_loss + recon_loss

        self.log("train_loss", loss, on_step=True, on_epoch=True,\
                prog_bar=True, logger=True)
        self.log("recon_loss", recon_loss, on_epoch=True,\
                prog_bar=True, logger=True)
        self.log("class_loss", class_loss, on_epoch=True,\
                prog_bar=True, logger=True)

        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch
        x = x.view(x.size(0), -1)
        x_hat, _, preds = self(x)
        class_loss = F.cross_entropy(preds, y)
        recon_loss = F.mse_loss(x, x_hat)
        loss = class_loss + recon_loss

        self.log("valid_loss", loss)

    def test_step(self, batch, batch_idx):
        x, y = batch
        _, _, logits = self(x)
        loss = F.cross_entropy(logits, y)
        preds = torch.argmax(logits, dim=1)
        acc = accuracy(preds, y)

        self.log("test_loss", loss, prog_bar=True)
        self.log("test_acc", acc, prog_bar=True)


class CIFAR10Autoencoder(BaseAutoEncoder):
    """
    This is an implementation of the autoencoder for CIFAR10
    """
    def __init__(self,
                input_dim: int=784,
                latent_dim: int=256,
                num_input_channels: int=3,
                base_channel_size: int=32,
                activation_fn: nn.modules.activation=nn.ReLU,
                perceptual_loss: bool=False,
                loss: Callable=None) -> None:
        """
        Args:
            input_dim (int): Dimension of the input to the autoencoder
            latent_dim (int): Dimension of the latent dimension
            num_input_channels (int): Number of input channels of the image.
            For CIFAR, this parameter is 3
            base_channel_size : Number of channels we use in the first 
            convolutional layers. Deeper layers might use a duplicate of it.
            activation_fn (nn.modules.activation): Activation function 
        """
        self.save_hyperparameters()
        self.input_dim     = input_dim
        self.latent_dim    = latent_dim
        self.num_input_channels = num_input_channels
        self.c_hid         = base_channel_size
        self.activation_fn = activation_fn

        if perceptual_loss:
            if not loss:
                raise AttributeError("Pass a callable loss to the attribute \
                                      loss when perceptual loss is True")
        super().__init__()
        self.perceptual_loss = perceptual_loss
        self.loss          = loss
        # self.linear        = nn.Sequential(
        #     nn.Linear(self.latent_dim, 2 * 16 * self.c_hid),
        #     self.activation_fn()
        # )
    
    def define_encoder(self) -> None:
        self.encoder = nn.Sequential(
            nn.Conv2d(self.num_input_channels, self.c_hid, kernel_size=3, padding=1, stride=2),  # 32x32 => 16x16
            self.activation_fn(),
            nn.Conv2d(self.c_hid, self.c_hid, kernel_size=3, padding=1),
            self.activation_fn(),
            nn.Conv2d(self.c_hid, 2 * self.c_hid, kernel_size=3, padding=1, stride=2),  # 16x16 => 8x8
            self.activation_fn(),
            nn.Conv2d(2 * self.c_hid, 2 * self.c_hid, kernel_size=3, padding=1),
            self.activation_fn(),
            nn.Conv2d(2 * self.c_hid, 2 * self.c_hid, kernel_size=3, padding=1, stride=2),  # 8x8 => 4x4
            self.activation_fn(),
            nn.Flatten(),  # Image grid to single feature vector
            nn.Linear(2 * 16 * self.c_hid, self.latent_dim),
        )

    def define_decoder(self) -> None:
        self.decoder = nn.Sequential(
            nn.Linear(self.latent_dim, 2 * 16 * self.c_hid),
            nn.Unflatten(1, (-1, 4, 4)),
            self.activation_fn(),
            nn.ConvTranspose2d(
                2 * self.c_hid, 2 * self.c_hid, kernel_size=3, output_padding=1, padding=1, stride=2
            ),  # 4x4 => 8x8
            self.activation_fn(),
            nn.Conv2d(2 * self.c_hid, 2 * self.c_hid, kernel_size=3, padding=1),
            self.activation_fn(),
            nn.ConvTranspose2d(2 * self.c_hid, self.c_hid, kernel_size=3, output_padding=1, padding=1, stride=2),  # 8x8 => 16x16
            self.activation_fn(),
            nn.Conv2d(self.c_hid, self.c_hid, kernel_size=3, padding=1),
            self.activation_fn(),
            nn.ConvTranspose2d(
                self.c_hid, self.num_input_channels, kernel_size=3, output_padding=1, padding=1, stride=2
            ),  # 16x16 => 32x32
            nn.Sigmoid(),  # The input images is scaled between -1 and 1, hence the output has to be bounded as well
        )
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z = self.encoder(x)
        # z = self.linear(z)
        # z = z.reshape(z.shape[0], -1, 4, 4)
        x_hat = self.decoder(z)

        return x_hat, z

    def training_step(self, batch, batch_idx):
        x, _ = batch
        x_hat, _ = self(x)
        if self.perceptual_loss:
            loss = self.loss(x, x_hat)
        else:
            loss = F.mse_loss(x, x_hat, reduction="none")
        loss = loss.sum(dim=[1, 2, 3]).mean(dim=[0])
        self.log("train_loss", loss)

        return loss
    
    def validation_step(self, batch, batch_idx):
        x, _ = batch
        x_hat, _ = self(x)
        if self.perceptual_loss:
            loss = self.loss(x, x_hat)
        else:
            loss = F.mse_loss(x, x_hat, reduction="none")
        loss = loss.sum(dim=[1, 2, 3]).mean(dim=[0])
        self.log("valid_loss", loss)
    
    def predict_step(self, batch, batch_idx):
        x, _ = batch
        
        return self(x)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.2, patience=20, min_lr=5e-5)
        
        return {"optimizer": optimizer, "lr_scheduler": scheduler, "monitor": "valid_loss"}


class CIFAR10VAE(BaseAutoEncoder):
    def __init__(self, enc_out_dim=512, latent_dim=256, input_height=32):

        self.save_hyperparameters()

        self.enc_out_dim  = enc_out_dim
        self.latent_dim   = latent_dim

        self.input_height = input_height
        super().__init__()

        # distribution parameters
        self.fc_mu = nn.Linear(enc_out_dim, latent_dim)
        self.fc_var = nn.Linear(enc_out_dim, latent_dim)

        # for the gaussian likelihood
        self.log_scale = nn.Parameter(torch.Tensor([0.0]))

    def define_encoder(self) -> None:
        self.encoder = resnet18_encoder(False, False)
    
    def define_decoder(self) -> None:
        self.decoder = resnet18_decoder(
            latent_dim=self.latent_dim, 
            input_height=self.input_height, 
            first_conv=False, 
            maxpool1=False
        )

    def get_z(self, x: torch.Tensor) -> torch.Tensor:
        x_encoded = self.encoder(x)
        mu, log_var = self.fc_mu(x_encoded), self.fc_var(x_encoded)

        # sample z from q
        std = torch.exp(log_var / 2)
        q = torch.distributions.Normal(mu, std)
        z = q.rsample()

        return z

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z = self.get_z(x)

        # decoded 
        x_hat = self.decoder(z)

        return x_hat, z

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

    def gaussian_likelihood(self, mean, logscale, sample):
        scale = torch.exp(logscale)
        dist = torch.distributions.Normal(mean, scale)
        log_pxz = dist.log_prob(sample)
        return log_pxz.sum(dim=(1, 2, 3))

    def kl_divergence(self, z, mu, std):
        # --------------------------
        # Monte carlo KL divergence
        # --------------------------
        # 1. define the first two probabilities (in this case Normal for both)
        p = torch.distributions.Normal(torch.zeros_like(mu), torch.ones_like(std))
        q = torch.distributions.Normal(mu, std)

        # 2. get the probabilities from the equation
        log_qzx = q.log_prob(z)
        log_pz = p.log_prob(z)

        # kl
        kl = (log_qzx - log_pz)
        kl = kl.sum(-1)
        return kl

    def training_step(self, batch, batch_idx):
        x, _ = batch

        # encode x to get the mu and variance parameters
        x_encoded = self.encoder(x)
        mu, log_var = self.fc_mu(x_encoded), self.fc_var(x_encoded)

        # sample z from q
        std = torch.exp(log_var / 2)
        q = torch.distributions.Normal(mu, std)
        z = q.rsample()

        # decoded 
        x_hat = self.decoder(z)

        # reconstruction loss
        recon_loss = self.gaussian_likelihood(x_hat, self.log_scale, x)

        # kl
        kl = self.kl_divergence(z, mu, std)

        # elbo
        elbo = (kl - recon_loss)
        elbo = elbo.mean()

        self.log_dict({
            'elbo': elbo,
            'kl': kl.mean(),
            'recon_loss': recon_loss.mean(), 
            'reconstruction': recon_loss.mean(),
            'kl': kl.mean(),
        })

        return elbo


class CIFAR10LightningAutoencoder(AE):
    def __init__(self, input_height=32):

        super().__init__(input_height=input_height)
        self.cls_model = CIFAR10Classifier.load_from_checkpoint("./lightning_logs/cifar10_classifier/checkpoints/epoch=49-step=35150.ckpt")
        self.cls_model.eval()

    def get_z(self, x: torch.Tensor) -> torch.Tensor:
        feats = self.encoder(x)
        z = self.fc(feats)

        return z
    
    def get_x_hat(self, z: torch.Tensor) -> torch.Tensor:
        x_hat = self.decoder(z)

        return x_hat
    
    def forward(self, x):
        feats = self.encoder(x)
        z = self.fc(feats)
        x_hat = self.decoder(z)

        return x_hat, z
    
    def step(self, batch, batch_idx):
        x, y = batch

        feats = self.encoder(x)
        z = self.fc(feats)
        x_hat = self.decoder(z)

        recon_loss = F.mse_loss(x_hat, x, reduction="mean")
        
        # classifier loss

        logits = self.cls_model(x)
        cls_loss = F.nll_loss(logits, y)

        loss = recon_loss + cls_loss

        return loss, {"loss": loss}


class CIFAR10NoisyLightningAutoencoder(AE):
    def __init__(self, input_height=32):

        super().__init__(input_height=input_height)
        self.cls_model = CIFAR10Classifier.load_from_checkpoint("./lightning_logs/cifar10_classifier/checkpoints/epoch=49-step=35150.ckpt")
        self.cls_model.eval()

    def get_z(self, x: torch.Tensor) -> torch.Tensor:
        feats = self.encoder(x)
        z = self.fc(feats)

        return z
    
    def get_x_hat(self, z: torch.Tensor) -> torch.Tensor:
        x_hat = self.decoder(z)

        return x_hat
    
    def forward(self, x):
        feats = self.encoder(x)
        z = self.fc(feats)
        z = z + torch.randn_like(z)
        x_hat = self.decoder(z)

        return x_hat, z
    
    def step(self, batch, batch_idx):
        x, y = batch

        feats = self.encoder(x)
        z = self.fc(feats)
        x_hat = self.decoder(z)

        recon_loss = F.mse_loss(x_hat, x, reduction="mean")
        
        # classifier loss

        logits = self.cls_model(x)
        cls_loss = F.nll_loss(logits, y)

        loss = recon_loss + cls_loss

        return loss, {"loss": loss}


class CelebAAutoencoderNew(BaseAutoEncoder):
    """
    This is an implementation of the autoencoder for CelebA
    """
    def __init__(self,
                 lr: float=1e-4,
                 activation_fn: nn.modules.activation=nn.ReLU) -> None:
        """
        Args:
            lr (float): learning rate
            activation_fn (nn.modules.activation): Activation function 
        """
        self.save_hyperparameters()
        self.lr = lr
        self.activation_fn = activation_fn
        super().__init__()

    def define_encoder(self):
        self.encoder = nn.Sequential(
            nn.Conv2d(3, 16, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(16, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(16, 16, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(16, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),

            nn.MaxPool2d(kernel_size=2, stride=2, padding=0, dilation=1, ceil_mode=False),

            nn.Conv2d(16, 32, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(32, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 32, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(32, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),

            nn.MaxPool2d(kernel_size=2, stride=2, padding=0, dilation=1, ceil_mode=False),

            nn.Conv2d(32, 64, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(64, 64, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),
            
            nn.MaxPool2d(kernel_size=2, stride=2, padding=0, dilation=1, ceil_mode=False),
            
            nn.Conv2d(64, 128, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 128, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),
        )

    def define_decoder(self):
        self.decoder = nn.Sequential(
            nn.ConvTranspose2d(128, 128, kernel_size=(2, 2), stride=(2, 2)),

            nn.Conv2d(128, 64, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(64, 64, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),

            nn.ConvTranspose2d(64, 64, kernel_size=(2, 2), stride=(2, 2)),

            nn.Conv2d(64, 32, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(32, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 32, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(32, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),

            nn.ConvTranspose2d(32, 32, kernel_size=(2, 2), stride=(2, 2)),

            nn.Conv2d(32, 16, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(16, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(16, 16, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1)),
            nn.BatchNorm2d(16, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True),
            nn.ReLU(inplace=True),

            nn.Conv2d(16, 3, kernel_size=(1, 1), stride=(1, 1)),
        )

    def forward(self, x):
        z = self.encoder(x)
        logits = self.decoder(z)
        outputs = logits

        # outputs = F.sigmoid(logits)
        # tanh = nn.Tanh()
        # outputs = tanh(logits)

        return outputs, z

    def training_step(self, batch, batch_idx):
        x, _ = batch
        x_hat, _ = self(x)
        # loss_bce = F.binary_cross_entropy_with_logits(x, x_hat, reduction="none")
        loss = F.mse_loss(x, x_hat, reduction="none")
        # loss = (0 * loss_bce) + (1.0 * loss_mse)
        loss = loss.sum(dim=[1, 2, 3]).mean(dim=[0])
        self.log("train_loss", loss)

        return loss
    
    def validation_step(self, batch, batch_idx):
        x, _ = batch
        x_hat, _ = self(x)
        loss = F.mse_loss(x, x_hat, reduction="none")
        loss = loss.sum(dim=[1, 2, 3]).mean(dim=[0])
        self.log("valid_loss", loss)
    
    def predict_step(self, batch, batch_idx):
        x, _ = batch

        return self(x)

    def configure_optimizers(self):

        return torch.optim.Adam(self.parameters(), lr=self.lr)
        # optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        # scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.2, patience=20, min_lr=5e-5)
        
        # return {"optimizer": optimizer, "lr_scheduler": scheduler, "monitor": "valid_loss"}


class ImagenetAutoencoder(BaseAutoEncoder):
    def __init__(self, configs) -> None:
        self.configs = configs
        super(ImagenetAutoencoder, self).__init__()

    def define_encoder(self):
        self.encoder = VGGEncoder(configs=self.configs, enable_bn=True)

    def define_decoder(self):
        self.decoder = VGGDecoder(configs=self.configs[::-1], enable_bn=True)


class GTSRBAutoencoder(BaseAutoEncoder):
    def __init__(self, lr):
        self.save_hyperparameters()
        self.lr = lr
        super().__init__()

    def define_encoder(self):
        self.encoder = nn.Sequential(
            nn.Conv2d(3, 16, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.Conv2d(16, 32, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.Conv2d(32, 32, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
        )

    def define_decoder(self):
        self.decoder = nn.Sequential(
            nn.ConvTranspose2d(32, 32, kernel_size=3, stride=2, padding=1, output_padding=1),
            nn.ReLU(),
            nn.ConvTranspose2d(32, 16, kernel_size=3, stride=2, padding=1, output_padding=1),
            nn.ReLU(),
            nn.ConvTranspose2d(16, 3, kernel_size=3, stride=2, padding=1, output_padding=1),
            nn.Tanh(),
        )
    
    def training_step(self, batch, batch_idx):
        x, _ = batch
        x_hat, z = self.forward(x)
        loss = F.mse_loss(x_hat, x)
        self.log('loss', loss, on_step=True, on_epoch=True)
        return {"loss": loss}
    
    def training_epoch_end(self, outputs):
        loss_values = [x['loss'] for x in outputs]
        avg_loss = torch.stack(loss_values).mean()
        self.log('avg_loss', avg_loss, on_epoch=True, prog_bar=True)
    
    def predict_step(self, batch, batch_idx):
        x, _ = batch

        return self(x)

    def validation_step(self, batch, batch_idx):
        x, _ = batch
        x_hat, z = self.forward(x)
        loss = F.mse_loss(x_hat, x)
        return loss

    def validation_epoch_end(self, outputs):
        avg_loss = torch.stack(outputs).mean()
        self.log("val_loss", avg_loss)

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.lr)
    

if __name__ == "__main__":

    import os
    def plot_recons(images, reshape):
        plt.figure(figsize=(20, 6))
        images = torch.Tensor(images).reshape(reshape)
        print(images.shape)
        # images = images.cpu().detach().numpy()
        grid = torchvision.utils.make_grid(images, nrow=10, normalize=False, range=(-1,1), )
        print(grid.shape)
        grid = grid.permute(1, 2, 0)
        grid = grid.cpu().detach().numpy()
        print(grid.shape)
        # print(grid[0] == grid[1])
        plt.imshow(grid)
        plt.axis('off')
        plt.show()
        plt.savefig("../img/mnist_ann_ae")

    def plot_recons_with_latent_codes(x, x_hat, z):
        plt.subplot(1,3,1)
        plt.title("Original")
        plt.imshow(x.reshape((28, 28)))

        plt.subplot(1,3,2)
        plt.title("Code")
        plt.imshow(z.reshape([z.shape[-1]//2,-1]).cpu().detach().numpy())

        plt.subplot(1,3,3)
        plt.title("Reconstructed")
        plt.imshow(x_hat.reshape((28, 28)))
        plt.show()
    
    """
    Testing GTSRB Autoencoder
    """
    # train_dataloader, valid_dataloader, test_dataloader = load_gtsrb(batch_size=1024, root="/scratch/itee/uqsswain/")
    
    # model = GTSRBAutoencoder(lr=1e-3)
    # trainer = pl.Trainer(
    #     max_epochs=50,
    #     accelerator="gpu",
    #     devices=1,
    #     default_root_dir="/scratch/itee/uqsswain/artifacts/spaa/autoencoders/gtsrb/"
    # )
    # trainer.fit(model, train_dataloader, valid_dataloader)
    # # model = CelebAAutoencoderNew.load_from_checkpoint(
    # #     "/scratch/itee/uqsswain/artifacts/spaa/autoencoders/celeba/lightning_logs/version_546284/checkpoints/epoch=19-step=11720.ckpt"
    # # )
    # train_dataloader, valid_dataloader, test_dataloader = load_gtsrb(batch_size=1, root="/scratch/itee/uqsswain/")
    # input_imgs, _ = next(iter(test_dataloader))
    # print(torch.min(input_imgs), torch.max(input_imgs))
    # model.eval()
    # reconst_imgs, z = model(input_imgs)
    # print(torch.min(reconst_imgs), torch.max(reconst_imgs))
    # print(z.shape)
    # print(torch.min(z), torch.max(z))

    # input_imgs   = input_imgs.reshape(3, 32, 32).detach()
    # reconst_imgs = reconst_imgs.reshape(3, 32, 32).detach()
    # fig, axis = plt.subplots(1,2)

    # axis[0].imshow(np.transpose(input_imgs, (1, 2, 0)))
    # axis[1].imshow(np.transpose(reconst_imgs, (1, 2, 0)))
    
    # plt.savefig(f"../plots/gtsrb/gtsrb_recon_tanh.png", dpi=1000)
    # plt.show()
    """
    Testing CelebA Autoencoder
    """
    # import torchsummary

    # # train_dataloader, valid_dataloader, test_dataloader = load_celeba(batch_size=128, root="/scratch/itee/uqsswain/")
    
    # # model = CelebAAutoencoderNew(lr=1e-5)
    # # # torchsummary.summary(model, (3, 128, 128))
    # # trainer = pl.Trainer(
    # #     max_epochs=20,
    # #     accelerator="gpu",
    # #     devices=2,
    # #     default_root_dir="/scratch/itee/uqsswain/artifacts/spaa/models/celeba/"
    # # )
    # # trainer.fit(model, train_dataloader, valid_dataloader)

    # model = CelebAAutoencoderNew.load_from_checkpoint("../lightning_logs/version_32/checkpoints/epoch=49-step=58600.ckpt")
    # # model = CelebAAutoencoderNew.load_from_checkpoint(
    # #     "/scratch/itee/uqsswain/artifacts/spaa/autoencoders/celeba/lightning_logs/version_546284/checkpoints/epoch=19-step=11720.ckpt"
    # # )
    # train_dataloader, valid_dataloader, test_dataloader = load_celeba(batch_size=1, root="/scratch/itee/uqsswain/")
    # input_imgs, _ = next(iter(test_dataloader))
    # print(torch.min(input_imgs), torch.max(input_imgs))
    # model.eval()
    # reconst_imgs, z = model(input_imgs)
    # print(torch.min(reconst_imgs), torch.max(reconst_imgs))
    # # print(z.shape)
    # print(torch.min(z), torch.max(z))

    # input_imgs   = input_imgs.reshape(3, 128, 128).detach()
    # reconst_imgs = reconst_imgs.reshape(3, 128, 128).detach()
    # fig, axis = plt.subplots(1,2)

    # axis[0].imshow(np.transpose(input_imgs, (1, 2, 0)))
    # axis[1].imshow(np.transpose(reconst_imgs, (1, 2, 0)))
    
    # plt.savefig(f"../plots/celeba/celeba_recon_sigmoid.png", dpi=1000)
    # plt.show()
    # visualize_cifar_reconstructions(input_imgs, reconst_imgs, file_name="celeba_ae_mse_recon")
    """
    Testing Imagenet autoencoder
    """
    # import os

    # # TODO: Use root in the dataloader file
    # config = get_configs()
    # # model = nn.DataParallel(ImagenetAutoencoder(config))
    # model = ImagenetAutoencoder(config)

    # # Testing
    # train_dataloader = load_imagenet_x(
    #     root="/home/harsh/scratch/datasets/IMAGENET/", batch_size=1
    # )
    # images, labels = next(iter(train_dataloader))
    # images, labels = images.to(device), labels.to(device)
    # checkpoint = torch.load("/home/harsh/scratch/models/imagenet-vgg16.pth")

    # new_state_dict = {}
    # for key, value in checkpoint["state_dict"].items():
    #     if key.startswith('module.'):
    #         new_key = key[7:] # remove "module." prefix
    #     else:
    #         new_key = key
    #     new_state_dict[new_key] = value

    # model.load_state_dict(new_state_dict)
    # model = model.to(device)
    # # model.eval()

    # with torch.no_grad():
    #     reconst_img, _ = model(images)
    # mean = torch.tensor([0.485, 0.456, 0.406]).to(device)
    # std = torch.tensor([0.229, 0.224, 0.225]).to(device)

    # denormalize = transforms.Normalize(mean=[-m/s for m, s in zip(mean, std)], std=[1/s for s in std])
    # images_normalized = denormalize(images)

    # reconst_img = torch.tensor((reconst_img.cpu().detach().numpy() * 255).astype(np.uint8)).to(device)
    # images_normalized = torch.tensor((images_normalized.cpu().detach().numpy() * 255).astype(np.uint8)).to(device)
    # # reconst_img = reconst_img * std + mean
    # # reconst_img = torch.clamp(reconst_img, 0, 1)
    # print(reconst_img.min(), reconst_img.max())
    # print(images_normalized.min(), images_normalized.max())
    # visualize_cifar_reconstructions(images_normalized, reconst_img, file_name="imagenet_vgg16_255")

    """
    Testing CIFAR autoencoder
    """
    # import os

    # train_dataloader, valid_dataloader, test_dataloader = load_cifar(
    #     root="/home/sweta/scratch/datasets/CIFAR10/", batch_size=128
    # )
    
    # model = CIFAR10Autoencoder()
    # trainer = pl.Trainer(max_epochs=100, gpus=1, default_root_dir="..")
    # # trainer.fit(model, train_dataloader, valid_dataloader)

    # # Testing
    # train_dataloader, valid_dataloader, test_dataloader = load_cifar(
    #     root="/home/sweta/scratch/datasets/CIFAR10/", batch_size=1
    # )
    # input_imgs, _ = next(iter(test_dataloader))
    # model = CIFAR10Autoencoder.load_from_checkpoint("../lightning_logs/version_39/checkpoints/epoch=99-step=35100.ckpt")
    # model.eval()
    # reconst_imgs, _ = model(input_imgs)

    # visualize_cifar_reconstructions(input_imgs, reconst_imgs, file_name="cifar10_ae_mse")

    """
    Testing CIFAR10 Lightning
    """
    model = CIFAR10LightningAutoencoder()
    # model = model.from_pretrained('cifar10-resnet18')

    train_dataloader, valid_dataloader, test_dataloader = load_cifar(
        root="/scratch/itee/uqsswain/", batch_size=256
    )

    trainer = pl.Trainer(max_epochs=50, accelerator="gpu", devices=1, default_root_dir="..")
    trainer.fit(model, train_dataloader, valid_dataloader)

    # Testing
    # trainer = pl.Trainer()
    # model = CIFAR10LightningAutoencoder.load_from_checkpoint("../lightning_logs/version_40/checkpoints/epoch=199-step=70200.ckpt")
    model.eval()

    train_dataloader, valid_dataloader, test_dataloader = load_cifar(
        root="/scratch/itee/uqsswain/", batch_size=10
    )
    input_imgs, _ = next(iter(test_dataloader))
    reconst_imgs, _ = model(input_imgs)

    visualize_cifar_reconstructions(input_imgs[-1].reshape(-1, 3, 32, 32), reconst_imgs[-1].reshape(-1, 3, 32, 32), file_name="cifar10_ae_mse_with_cls_loss")

    """
    Testing MNIST autoencoder
    """
    # train_dataloader, valid_dataloader, test_dataloader = load_mnist(
    #     root="~/scratch/datasets/MNIST/", batch_size=128
    # )
    # model = ANNAutoencoder()
    # trainer = pl.Trainer(max_epochs=20, accelerator="cuda", default_root_dir="..")
    # # trainer.fit(model, train_dataloader, valid_dataloader)    

    # model = ANNAutoencoder.load_from_checkpoint("../lightning_logs/mnist_ae_mse/checkpoints/checkpoint.ckpt")
    # model.eval()

    # # Reconstruct
    # train_dataloader, valid_dataloader, test_dataloader = load_mnist(
    #     root="~/scratch/datasets/MNIST/", batch_size=10
    # )
    # x, _ = next(iter(test_dataloader))
    # x_hat, _ = model(x)
    # images = torch.cat((x, x_hat), 0)
    # plot_recons(images=images, reshape=(-1, 1, 28, 28))

    """
    Testing MNIST CNN autoencoder
    """
    # os.environ["CUDA_VISIBLE_DEVICES"] = "5"
    # train_dataloader, valid_dataloader, test_dataloader = load_mnist(
    #     root="~/scratch/datasets/MNIST/", batch_size=128
    # )
    # model = MNISTCNNAutoencoder()
    # trainer = pl.Trainer(max_epochs=20, accelerator="gpu", default_root_dir="..")
    # # trainer.fit(model, train_dataloader, valid_dataloader) 

    # model = MNISTCNNAutoencoder.load_from_checkpoint("../lightning_logs/version_42/checkpoints/epoch=19-step=8580.ckpt")
    # model.eval()

    # # Reconstruct
    # train_dataloader, valid_dataloader, test_dataloader = load_mnist(
    #     root="~/scratch/datasets/MNIST/", batch_size=10
    # )
    # x, _ = next(iter(test_dataloader))
    # x = x.reshape((-1, 1, 28, 28))
    # x_hat, _ = model(x)
    # images = torch.cat((x, x_hat), 0)
    # plot_recons(images=images, reshape=(-1, 1, 28, 28))
