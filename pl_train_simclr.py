import pytorch_lightning as pl
import torch
import torchvision.datasets as datasets
from torch.utils.data import DataLoader, Dataset
import argparse
import torch.nn.functional as F
from einops import repeat

from torchvision import transforms as T
from pytorch_lightning.callbacks import LearningRateMonitor
from torch import nn
import random
import torch.distributed as dist
from torchvision import models as torchvision_models

import utils
import json
import math
import vision_transformer as vits
from byol_pytorch import NetWrapper
import fine_tune

from PIL import Image


def default(val, def_val):
    return def_val if val is None else val


def count_parameters(m):
    return sum(p.numel() for p in m.parameters())


class RandomApply(nn.Module):
    def __init__(self, fn, p):
        super().__init__()
        self.fn = fn
        self.p = p

    def forward(self, x):
        if random.random() > self.p:
            return x
        return self.fn(x)


def loss_fn(x, y):
    x = F.normalize(x, dim=-1, p=2)
    y = F.normalize(y, dim=-1, p=2)
    return (2 - 2 * (x * y).sum(dim=-1)).mean()


class PLLearner(pl.LightningModule):
    def __init__(self, student, length, val_loader, embed_dim, args):
        super().__init__()
        # self.save_hyperparameters()
        self.ratio = args.ratio
        self.st_inter = args.st_inter
        self.t_inter = args.t_inter
        self.temperature = args.temperature

        self.student = NetWrapper(student, embed_dim, args, prediction=False, intermediate=self.st_inter)

        print(f"Student is built: {args.arch} network.")

        # ============ preparing optimizer ... ============
        params_groups = utils.get_params_groups(self.student)
        if args.optimizer == "adamw":
            self.optimizer = torch.optim.AdamW(params_groups)  # to use with ViTs
        elif args.optimizer == "sgd":
            self.optimizer = torch.optim.SGD(params_groups, lr=0, momentum=0.9)  # lr is set by scheduler
        elif args.optimizer == "lars":
            self.optimizer = utils.LARS(params_groups)  # to use with convnet and large batches

        length = math.ceil(length / (args.accumulate * torch.cuda.device_count()))

        # ============ init schedulers ... ============
        self.lr_schedule = utils.cosine_scheduler(
            # args.lr * (args.accumulate * args.batch_size_per_gpu * torch.cuda.device_count()) / 256.,  # linear scaling rule
            args.lr * args.total_batch / 256.,
            # args.min_lr * (args.accumulate * args.batch_size_per_gpu * torch.cuda.device_count()) / 256.,
            args.min_lr,
            args.epochs, length,
            warmup_epochs=args.warmup_epochs,
        )
        self.wd_schedule = utils.cosine_scheduler(
            args.weight_decay,
            args.weight_decay_end,
            args.epochs, length,
        )

        # print(length)
        # momentum parameter is increased to 1. during training with a cosine schedule
        print(f"Loss, optimizer and schedulers ready.")

        self.val_loader = val_loader

        self.aug1 = torch.nn.Sequential(
            RandomApply(
                T.ColorJitter(0.8, 0.8, 0.8, 0.2),
                p=0.3
            ),
            T.RandomGrayscale(p=0.2),
            T.RandomHorizontalFlip(),
            RandomApply(
                T.GaussianBlur((3, 3), (1.0, 2.0)),
                p=0.2
            ),
            T.RandomResizedCrop((args.image_size, args.image_size)),
            T.Normalize(
                mean=torch.tensor([0.485, 0.456, 0.406]),
                std=torch.tensor([0.229, 0.224, 0.225])),
        )

        self.aug2 = self.aug1
        self.criterion = nn.CrossEntropyLoss()

        self.labels = None
        self.label = None
        self.mask = None

        # self.fp16_scaler = None
        # if args.use_fp16:
        #     self.fp16_scaler = torch.cuda.amp.GradScaler()

    def configure_optimizers(self):
        return [self.optimizer]

    def forward(self, x):
        image_one, image_two = self.aug1(x), self.aug2(x)
        return self.student(image_one), self.student(image_two)

    def info_nce_loss(self, features):
        if self.labels is None:
            b, _ = features.shape
            self.labels = torch.cat([torch.arange(b/2) for i in range(2)], dim=0)
            self.labels = (self.labels.unsqueeze(0) == self.labels.unsqueeze(1)).float()
            self.labels.to(self.device)

        features = F.normalize(features, dim=1)

        similarity_matrix = torch.matmul(features, features.T)

        if self.mask is None:
            self.mask = torch.eye(self.labels.shape[0], dtype=torch.bool, device=self.device)
        labels = self.labels[~self.mask].view(self.labels.shape[0], -1)
        similarity_matrix = similarity_matrix[~self.mask].view(similarity_matrix.shape[0], -1)

        positives = similarity_matrix[labels.bool()].view(labels.shape[0], -1)

        negatives = similarity_matrix[~labels.bool()].view(similarity_matrix.shape[0], -1)

        logits = torch.cat([positives, negatives], dim=1)
        if self.label is None:
            self.label = torch.zeros(logits.shape[0], dtype=torch.long, device=self.device)

        logits = logits / self.temperature
        return self.criterion(logits, self.label)

    def info_nce_loss_intermediate(self, layer_features, output):
        b = layer_features.shape[0]

        labels = torch.cat([torch.arange(b/11, dtype=torch.long, device=self.device) for _ in range(11)], dim=0)

        layer_features = F.normalize(layer_features, dim=1)
        output = F.normalize(output, dim=1).detach()

        similarity_matrix = torch.matmul(layer_features, output.T)

        logits = similarity_matrix / 0.1
        loss = self.criterion(logits, labels)
        return loss

    def training_step(self, batch, batch_idx):
        images = batch[0]
        batch_size = images.shape[0]

        # with torch.cuda.amp.autocast(self.fp16_scaler is not None):
        student_output1, student_output2 = self.forward(images)

        # if self.st_inter != self.t_inter:
        #     teacher_output1 = repeat(teacher_output1.unsqueeze(0), '() b e -> (d b) e', d=12)
        #     teacher_output2 = repeat(teacher_output2.unsqueeze(0), '() b e -> (d b) e', d=12)

        loss_mid = 0.0
        if self.st_inter != self.t_inter:
            student_mid1, student_output1 = torch.split(student_output1, [batch_size * 11, batch_size], dim=0)
            student_mid2, student_output2 = torch.split(student_output2, [batch_size * 11, batch_size], dim=0)
            loss_mid = self.info_nce_loss_intermediate(student_mid1, student_output2) + self.info_nce_loss_intermediate(student_mid2, student_output1)
        loss_output = self.info_nce_loss(torch.cat((student_output1, student_output2), dim=0))
        if self.ratio > 0:
            ratio = self.ratio
        else:
            ratio = 11
        loss = loss_output + ratio*loss_mid

        self.logger.experiment.add_scalar('loss', loss.detach().item(), self.global_step)

        return {'loss': loss}

    def on_after_backward(self):
        for i, param_group in enumerate(self.optimizer.param_groups):
            param_group["lr"] = self.lr_schedule[self.global_step]
            if i == 0:
                self.logger.experiment.add_scalar('lr', self.lr_schedule[self.global_step], self.global_step)
                param_group["weight_decay"] = self.wd_schedule[self.global_step]

    # def on_before_zero_grad(self, _):
    #     m = self.momentum_schedule[self.global_step]
    #     for current_params, ma_params in zip(self.student.net.parameters(), self.teacher.net.parameters()):
    #         old_weight, up_weight = ma_params.data, current_params.data
    #         ma_params.data = old_weight * m + (1 - m) * up_weight
    #
    #     if self.st_inter != self.t_inter:
    #         for current_params, ma_params in zip(self.student.projector[-1].parameters(), self.teacher.projector.parameters()):
    #             old_weight, up_weight = ma_params.data, current_params.data
    #             ma_params.data = old_weight * m + (1 - m) * up_weight
    #     else:
    #         for current_params, ma_params in zip(self.student.projector.parameters(), self.teacher.projector.parameters()):
    #             old_weight, up_weight = ma_params.data, current_params.data
    #             ma_params.data = old_weight * m + (1 - m) * up_weight

    @torch.no_grad()
    def validation_step(self, batch, batch_idx):
        x, label = batch

        features = self.student.get_representation(x).detach().cpu()
        return {'features': features, 'labels': label}

    @torch.no_grad()
    def validation_step_end(self, batch_parts):
        # print(batch_parts)
        features = batch_parts['features']
        labels = batch_parts['labels']

        return features, labels

    @torch.no_grad()
    def validation_epoch_end(self, outs):
        train_features = torch.cat([f[0] for f in outs]).to(self.device)
        gather_t = [torch.ones_like(train_features) for _ in range(dist.get_world_size())]
        dist.all_gather(gather_t, train_features)
        train_features = torch.cat(gather_t).to(self.device)
        train_features = F.normalize(train_features, dim=1).t()

        train_labels = torch.cat([f[1] for f in outs])
        gather_t = [torch.ones_like(train_labels) for _ in range(dist.get_world_size())]
        dist.all_gather(gather_t, train_labels)
        train_labels = torch.cat(gather_t).to(self.device)

        k = 20
        num_classes = 1000
        retrieval_one_hot = torch.zeros(k, num_classes).to(self.device)
        top1, top5, total = 0.0, 0.0, 0
        # print("train_features", train_features)
        # print(len(self.val_loader))

        for batch in self.val_loader:
            features = self.student.get_representation(batch[0].to(self.device))
            features = F.normalize(features, dim=1)
            # print("features", features)
            targets = batch[1].to(self.device)
            # print(targets)

            batch_size = targets.shape[0]

            similarity = torch.mm(features, train_features)
            # print("similarity", similarity)
            distances, indices = similarity.topk(k, largest=True, sorted=True)
            distances = distances.to(self.device)
            indices = indices.to(self.device)
            # print("distances", distances)
            # print("indices", indices)
            candidates = train_labels.view(1, -1).expand(batch_size, -1)
            # print("candidates", candidates)
            retrieved_neighbors = torch.gather(candidates, 1, indices)

            retrieval_one_hot.resize_(batch_size * k, num_classes).zero_()
            retrieval_one_hot.scatter_(1, retrieved_neighbors.view(-1, 1), 1.0)
            # print("retrieval_one_hot", retrieval_one_hot)
            distances_transform = distances.clone().div_(0.07).exp_()
            # print("distances_transform", distances_transform)
            probs = torch.sum(
                torch.mul(
                    retrieval_one_hot.view(batch_size, -1, num_classes),
                    distances_transform.view(batch_size, -1, 1),
                ),
                1,
            )
            # print("probs", probs)
            _, predictions = probs.sort(1, True)
            # print("prediction", predictions)

            correct = predictions.eq(targets.data.view(-1, 1))
            top1 = top1 + correct.narrow(1, 0, 1).sum().item()
            top5 = top5 + correct.narrow(1, 0, 5).sum().item()
            total += targets.size(0)

        top1 = top1 * 100.0 / total
        top5 = top5 * 100.0 / total
        # print(top1, top5)
        if utils.get_rank() == 0:
            print(f"Epoch: {self.current_epoch}  top1: {top1}  top5: {top5}")
            total_acc_t1.append(top1)
            total_acc_t5.append(top5)
        self.logger.experiment.add_scalar('top1', top1, self.current_epoch)
        self.logger.experiment.add_scalar('top5', top5, self.current_epoch)


# class Monitor(pl.Callback):
#     def on_train_batch_start(self, pl_trainer, pl_module, batch, batch_idx, dataloader_idx):
#         if batch_idx % 100 == 0:
#             pl_logger = pl_trainer.logger
#             pl_logger.experiment.add_histogram("input", batch, global_step=pl_trainer.global_step)
#
#

def expand_greyscale(t):
    return t.expand(3, -1, -1)

torchvision_archs = sorted(name for name in torchvision_models.__dict__
                           if name.islower() and not name.startswith("__")
                           and callable(torchvision_models.__dict__[name]))

total_acc_t1 = []
total_acc_t5 = []

def main(args):
    dataset = None
    dataset_train = None
    dataset_val = None
    fine_dataset = None

    image_size = 96 if args.dataset == "stl10" else 224
    # pretrain_transform = DataAugmentationDINO(
    #     args.global_crops_scale,
    #     args.local_crops_scale,
    #     args.local_crops_number
    # )
    pretrain_transform = T.Compose([
        T.Resize((256, 256), interpolation=Image.BICUBIC),
        # T.CenterCrop(image_size),
        T.ToTensor(),
        # T.Lambda(expand_greyscale)
    ])
    fine_transform = T.Compose([
        T.RandomResizedCrop(224),
        T.RandomHorizontalFlip(),
        T.ToTensor(),
        T.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
    ])
    val_transform = T.Compose([
        T.Resize((256, 256), interpolation=3),
        T.CenterCrop((image_size, image_size)),
        T.ToTensor(),
        T.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
    ])

    if args.dataset == "stl10":
        dataset = datasets.STL10(args.data, split='unlabeled', download=True, transform=pretrain_transform)
        dataset_train = datasets.STL10(args.data, split='train', download=True, transform=val_transform)
        dataset_val = datasets.STL10(args.data, split='test', download=True, transform=val_transform)
    elif args.dataset == "imagenet":
        path = 'dataset'
        # path = '/data/dataset/imagenet_cls_loc/CLS_LOC/ILSVRC2015/Data/CLS-LOC'
        dataset = datasets.ImageFolder(
            path + '/train',
            pretrain_transform
        )
        dataset_train = datasets.ImageFolder(
            path + '/train',
            val_transform
        )
        dataset_val = datasets.ImageFolder(
            path + '/val',
            val_transform
        )
        fine_dataset = datasets.ImageFolder(
            path + '/train',
            fine_transform
        )
    else:
        assert "error"
    # sampler = torch.utils.data.DistributedSampler(dataset, shuffle=True)
    data_loader = DataLoader(
        dataset,
        batch_size=args.batch_size_per_gpu,
        shuffle=True,
        num_workers=args.num_workers,
        drop_last=True,
    )
    fine_loader = DataLoader(
        fine_dataset,
        # Subset(fine_dataset, np.arange(64)),
        batch_size=args.batch_size_per_gpu,
        shuffle=True,
        num_workers=args.num_workers,
        drop_last=True,
        pin_memory=True,
    )
    # sampler_train = torch.utils.data.DistributedSampler(dataset_train, shuffle=False)
    train_loader = DataLoader(
        dataset_train,
        batch_size=args.batch_size_per_gpu,
        # sampler=sampler_train,
        shuffle=False,
        num_workers=args.num_workers,
    )
    # sampler_val = torch.utils.data.DistributedSampler(dataset_val, shuffle=False)
    val_loader = DataLoader(
        dataset_val,
        batch_size=args.batch_size_per_gpu,
        shuffle=False,
        num_workers=args.num_workers,
    )
    print("loaded dataset!")

    if args.arch in vits.__dict__.keys():
        student = vits.__dict__[args.arch](
            patch_size=args.patch_size,
            drop_path_rate=0.1,  # stochastic depth
        )
        # teacher = vits.__dict__[args.arch](patch_size=args.patch_size)
        embed_dim = student.embed_dim
    # otherwise, we check if the architecture is in torchvision models
    elif args.arch in torchvision_models.__dict__.keys():
        student = torchvision_models.__dict__[args.arch]()
        # teacher = torchvision_models.__dict__[args.arch]()
        embed_dim = student.fc.weight.shape[1]
    else:
        print(f"Unknow architecture: {args.arch}")

    # student = torchvision_models.resnet18(pretrained=False, num_classes=args.out_dim)
    # teacher = torchvision_models.resnet18(pretrained=False, num_classes=args.out_dim)

    lr = args.lr * 10000
    min_lr = args.min_lr * 10000
    total_batch = torch.cuda.device_count() * args.accumulate * args.batch_size_per_gpu * args.multi_node
    clip = args.clip_grad

    args.image_size = image_size
    args.total_batch = total_batch

    learner = PLLearner(student, len(data_loader), val_loader, embed_dim, args)

    logger = pl.loggers.TensorBoardLogger(args.board_path, name=args.name + "_{}e/{}_{}_{}_{}_{}_{}".format(args.epochs, lr, min_lr, total_batch, clip, args.weight_decay, args.weight_decay_end))
    lr_monitor = LearningRateMonitor(logging_interval='step')
    trainer = pl.Trainer(
        gpus=torch.cuda.device_count(),
        max_epochs=args.max_epochs,
        default_root_dir="output/vit.model",
        accelerator=args.accelerator,
        logger=logger,
        num_sanity_val_steps=0,
        gradient_clip_val=args.clip_grad,
        accumulate_grad_batches=args.accumulate,
        check_val_every_n_epoch=args.val_interval,
        sync_batchnorm=True,
        callbacks=[lr_monitor],
        progress_bar_refresh_rate=0
    )

    trainer.fit(learner, data_loader, train_loader)

    if utils.get_rank() == 0:
        print("top1", total_acc_t1)
        print("best top1", max(total_acc_t1))
        print("top5", total_acc_t5)
        print("best top5", max(total_acc_t5))

    learner.student.eval()

    tuner = fine_tune.Tuner(learner.student, embed_dim, total_batch)
    fine_trainer = pl.Trainer(
        gpus=torch.cuda.device_count(),
        max_epochs=100,
        default_root_dir="output/vit.model",
        accelerator="ddp",
        logger=logger,
        num_sanity_val_steps=0,
        accumulate_grad_batches=args.accumulate,
        check_val_every_n_epoch=10,
        sync_batchnorm=True,
        callbacks=[lr_monitor],
        progress_bar_refresh_rate=0
    )
    fine_trainer.fit(tuner, fine_loader, val_loader)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='simclr')

    parser.add_argument('--load_json',
                        help='Load settings from file in json format. Command line options override values in file.')

    parser.add_argument('--lr', '-l', default=1e-5, type=float, help='learning rate')
    parser.add_argument('--epochs', '-e', type=int, default=300, help="epochs for scheduling")
    parser.add_argument('--max_epochs', type=int, default=100, help="epochs for actual training")
    parser.add_argument('--batch_size_per_gpu', '-b', type=int, default=256, help="batch size")
    parser.add_argument('--num_workers', '-n', type=int, default=16, help='number of workers')
    parser.add_argument('--board_path', '-bp', default='./log', type=str, help='tensorboard path')
    parser.add_argument('--accumulate', default=1, type=int, help='accumulate gradient')
    parser.add_argument('--mlp_hidden', default=4096, type=int, help='mlp hidden dimension')
    parser.add_argument('--ratio', default=1, type=int, help='loss ratio of layer2output')
    parser.add_argument('--up', default=12, type=int, help='layer2high skip layer')
    parser.add_argument('--st_inter', default=False, type=bool, help='intermediate representation of student')
    parser.add_argument('--t_inter', default=False, type=bool, help='intermediate representation of teacher')
    parser.add_argument('--temperature', default=0.1, type=float, help='temperature for infoNCE')

    parser.add_argument('--data', '-d', metavar='DIR', default='../dataset',
                        help='path to dataset')
    parser.add_argument('--dataset', '-ds', default='stl10',
                        help='dataset name', choices=['stl10', 'cifar10', 'imagenet'])
    parser.add_argument('--name', help='name for tensorboard')
    parser.add_argument('--val_interval', default=1, type=int, help='validation epoch interval')
    parser.add_argument('--accelerator', default='ddp', type=str,
                        help='ddp for multi-gpu or node, ddp2 for across negative samples')

    # # Multi-crop parameters
    # parser.add_argument('--global_crops_scale', type=float, nargs='+', default=(0.4, 1.),
    #                     help="""Scale range of the cropped image before resizing, relatively to the origin image.
    #     Used for large global view cropping. When disabling multi-crop (--local_crops_number 0), we
    #     recommand using a wider range of scale ("--global_crops_scale 0.14 1." for example)""")
    # parser.add_argument('--local_crops_number', type=int, default=8, help="""Number of small
    #     local views to generate. Set this parameter to 0 to disable multi-crop training.
    #     When disabling multi-crop we recommend to use "--global_crops_scale 0.14 1." """)
    # parser.add_argument('--local_crops_scale', type=float, nargs='+', default=(0.05, 0.4),
    #                     help="""Scale range of the cropped image before resizing, relatively to the origin image.
    #     Used for small local view cropping of multi-crop.""")

    parser.add_argument("--warmup_epochs", default=10, type=int,
                        help="Number of epochs for the linear learning-rate warm up.")
    parser.add_argument('--min_lr', type=float, default=1e-6, help="""Target LR at the
            end of optimization. We use a cosine LR schedule with linear warmup.""")
    # parser.add_argument('--freeze_last_layer', default=1, type=int, help="""Number of epochs
    #     during which we keep the output layer fixed. Typically doing so during
    #     the first epoch helps training. Try increasing this value if the loss does not decrease.""")
    parser.add_argument('--weight_decay', type=float, default=0.04, help="""Initial value of the
            weight decay. With ViT, a smaller value at the beginning of training works well.""")
    parser.add_argument('--weight_decay_end', type=float, default=0.4, help="""Final value of the
            weight decay. We use a cosine schedule for WD and using a larger decay by
            the end of training improves performance for ViTs.""")
    parser.add_argument('--clip_grad', type=float, default=3.0, help="""Maximal parameter
            gradient norm if using gradient clipping. Clipping with norm .3 ~ 1.0 can
            help optimization for larger ViT architectures. 0 for disabling.""")

    # # Temperature teacher parameters
    # parser.add_argument('--warmup_teacher_temp', default=0.04, type=float,
    #                     help="""Initial value for the teacher temperature: 0.04 works well in most cases.
    #     Try decreasing it if the training loss does not decrease.""")
    # parser.add_argument('--teacher_temp', default=0.04, type=float, help="""Final value (after linear warmup)
    #     of the teacher temperature. For most experiments, anything above 0.07 is unstable. We recommend
    #     starting with the default value of 0.04 and increase this slightly if needed.""")
    # parser.add_argument('--warmup_teacher_temp_epochs', default=0, type=int,
    #                     help='Number of warmup epochs for the teacher temperature (Default: 30).')
    # parser.add_argument('--norm_last_layer', default=True, type=utils.bool_flag,
    #                     help="""Whether or not to weight normalize the last layer of the DINO head.
    #     Not normalizing leads to better performance but can make the training unstable.
    #     In our experiments, we typically set this paramater to False with vit_small and True with vit_base.""")

    # Model parameters
    parser.add_argument('--arch', default='vit_small', type=str,
                        choices=['vit_tiny', 'vit_small', 'vit_base', 'deit_base', 'deit_tiny',
                                 'deit_small'] + torchvision_archs,
                        help="""Name of architecture to train. For quick experiments with ViTs,
                we recommend using vit_tiny or vit_small.""")
    parser.add_argument('--patch_size', default=16, type=int, help="""Size in pixels
            of input square patches - default 16 (for 16x16 patches). Using smaller
            values leads to better performance but requires more memory. Applies only
            for ViTs (vit_tiny, vit_small and vit_base). If <16, we recommend disabling
            mixed precision training (--use_fp16 false) to avoid unstabilities.""")
    parser.add_argument('--out_dim', default=512, type=int, help="""Dimensionality of
            the DINO head output. For complex and large datasets large values (like 65k) work well.""")
    parser.add_argument('--div', default=4, type=int, help="dividing hidden dimensions of mlp1")
    parser.add_argument('--momentum_teacher', default=0.996, type=float, help="""Base EMA
            parameter for teacher update. The value is increased to 1 during training with cosine schedule.
            We recommend setting a higher value with small batches: for example use 0.9995 with batch size of 256.""")
    parser.add_argument('--use_bn_in_head', default=False, type=utils.bool_flag,
                        help="Whether to use batch normalizations in projection head (Default: False)")

    hparam = parser.parse_args()
    if hparam.load_json:
        with open(hparam.load_json, 'rt') as f:
            t_args = argparse.Namespace()
            t_args.__dict__.update(json.load(f))
            hparam = parser.parse_args(namespace=t_args)

    main(hparam)
