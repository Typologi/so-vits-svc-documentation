"""
    训练多人 VITS 模型
"""
import logging
import os
import platform

import torch
from torch.nn import functional as F
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import torch.multiprocessing as mp
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.cuda.amp import autocast, GradScaler

import commons
import utils
from data_utils import (
    TextAudioSpeakerLoader,
    TextAudioSpeakerCollate,
    DistributedBucketSampler
)
from models import (
    SynthesizerTrn,
    MultiPeriodDiscriminator,
)
from losses import (
    generator_loss,
    discriminator_loss,
    feature_loss,
    kl_loss
)
from mel_processing import mel_spectrogram_torch, spec_to_mel_torch

torch.backends.cudnn.benchmark = True
global_step = 0


def main():
    # 获取 matplotlib 的日志，设置级别为 WARNING，防止出现一堆 DEBUG
    matplotlib_logger = logging.getLogger("matplotlib")
    if matplotlib_logger is not None:
        matplotlib_logger.setLevel(logging.WARNING)
    """假设单节点多GPU训练，设备没 GPU 不允许训练"""
    assert torch.cuda.is_available(), "CPU training is not allowed."

    """查看当前设备 GPU 总数"""
    n_gpus = torch.cuda.device_count()
    """设置主节点地址 PS: 这个端口不是MC端口吗"""
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '25565'

    """
        从命令行中读取指令
        指令结构: [-c 模型结构.json] [-m 模型存放位置]
    """
    hps = utils.get_hparams()
    """
    以 Pytorch 多线程姿态出击，
    启动的线程数量与系统中的显卡数量一致，这也就意味着可能的话，可以使用多张显卡 Train 以成倍地加快训练过程
    一般情况下只会启动一个主线程来负责模型训练以及其他杂活
    """
    mp.spawn(run, nprocs=n_gpus, args=(n_gpus, hps,))


def run(rank, n_gpus, hps):
    # 声明全局步数
    global global_step
    # 如果当前线程为主线程，在该线程上初始化 Log，同时产生日志对象
    if rank == 0:
        logger = utils.get_logger(hps.model_dir)
        logger.info(hps)
        # 检查当前 .git 仓库的 hash，如果不一致则报警
        utils.check_git_hash(hps.model_dir)
        # 创建新的 TensorBoard Writer，指定写出文件夹为模型存放位置的文件夹
        writer = SummaryWriter(log_dir=hps.model_dir)
        # 同上，不过是用于模型 eval 时的日志
        writer_eval = SummaryWriter(log_dir=os.path.join(hps.model_dir, "eval"))

    # 根据操作系统创建不同的进程组，采取本机环境以使用本机多张显卡。
    if platform.system() == 'Windows':
        # 如果是 Windows 则采取 Gloo 后端实现
        logger.info('Running on windows, launching gloo as distribute system backend')
        dist.init_process_group(backend='gloo', init_method='env://', world_size=n_gpus, rank=rank)
    elif platform.system() == 'Linux':
        # 如果是 Linux 则采取 Nccl 实现
        logger.info('Running on Linux, launching gloo as distribute system backend')
        dist.init_process_group(backend='nccl', init_method='env://', world_size=n_gpus, rank=rank)
    elif platform.system() == 'Darwin':
        # MacOS 不支持
        logger.error('MacOS is not supported yet')
        return
    else:
        # 其他系统不支持
        logger.info(f'Unsupported operating system: {platform.system()}')
        return
    # 加载手动设置的训练种子
    torch.manual_seed(hps.train.seed)
    # 根据 rank 设置使用的显卡
    torch.cuda.set_device(rank)

    """
        在这里加载训练数据集
        使用配置文件中声明的 training_files 的 txt 引导文件加载数据，同时传入一些从json中读取的配置项目
        
        text_cleaners:  SoVits 不适用
        max_wav_value: 最大波值
        sampling_rate: 训练音频采样率
        filter_length: 短时傅里叶变换单片音频长度
        hop_length: 短时傅里叶变化滑动长度
        win_length: 滑动窗口大小
        sampling_rate: 数据集中音频采样率 
        
        cleaned_text: SoVits 不适用
        add_blank:
        min_text_len: 最小文本长度 ( Sovits 中不是文本 )
        max_text_len: 最大文本长度 ( Sovits 中不是文本 )
    """
    train_dataset = TextAudioSpeakerLoader(hps.data.training_files, hps.data)

    # 分布式训练数据集采样器，用于使得输入训练数据的长度一致
    train_sampler = DistributedBucketSampler(
        train_dataset,
        hps.train.batch_size,
        [32, 300, 400, 500, 600, 700, 800, 900, 1000],
        num_replicas=n_gpus,
        rank=rank,
        shuffle=True)

    collate_fn = TextAudioSpeakerCollate()
    train_loader = DataLoader(train_dataset, num_workers=8, shuffle=False, pin_memory=True,
                              collate_fn=collate_fn, batch_sampler=train_sampler)
    # 如果是主进程进程，则创建测试集
    if rank == 0:
        eval_dataset = TextAudioSpeakerLoader(hps.data.validation_files, hps.data)
        eval_loader = DataLoader(eval_dataset, num_workers=8, shuffle=False,
                                 batch_size=hps.train.batch_size, pin_memory=True,
                                 drop_last=False, collate_fn=collate_fn)
    """
        初始化生成器模型,GAN
    """
    net_g = SynthesizerTrn(
        178,
        hps.data.filter_length // 2 + 1,
        hps.train.segment_size // hps.data.hop_length,
        # 多人模型中的新增代码，用于指定训练集中一共有多少个说话人
        n_speakers=hps.data.n_speakers,
        # 多人模型中的新增代码
        **hps.model).cuda(rank)
    """
        初始化辨别器模型,GAN
    """
    net_d = MultiPeriodDiscriminator(hps.model.use_spectral_norm).cuda(rank)
    """
        为生成器设置优化函数
    """
    optim_g = torch.optim.AdamW(
        net_g.parameters(),
        hps.train.learning_rate,
        betas=hps.train.betas,
        eps=hps.train.eps)
    """
        为鉴别器设置优化函数
    """
    optim_d = torch.optim.AdamW(
        net_d.parameters(),
        hps.train.learning_rate,
        betas=hps.train.betas,
        eps=hps.train.eps)
    """
        使用并行数据
    """
    net_g = DDP(net_g, device_ids=[rank])
    net_d = DDP(net_d, device_ids=[rank])

    """
        尝试加载已经存在的模型数据，在此基础上继续训练
        会加载拥有最大序号的 G 和 D 模型数据
    """
    try:
        _, _, _, epoch_str = utils.load_checkpoint(utils.latest_checkpoint_path(hps.model_dir, "G_*.pth"), net_g,
                                                   optim_g)
        _, _, _, epoch_str = utils.load_checkpoint(utils.latest_checkpoint_path(hps.model_dir, "D_*.pth"), net_d,
                                                   optim_d)
        global_step = (epoch_str - 1) * len(train_loader)
    except:
        epoch_str = 1
        global_step = 0
    scheduler_g = torch.optim.lr_scheduler.ExponentialLR(optim_g, gamma=hps.train.lr_decay, last_epoch=epoch_str - 2)
    scheduler_d = torch.optim.lr_scheduler.ExponentialLR(optim_d, gamma=hps.train.lr_decay, last_epoch=epoch_str - 2)

    scaler = GradScaler(enabled=hps.train.fp16_run)

    """
        开始训练
    """
    for epoch in range(epoch_str, hps.train.epochs + 1):
        if rank == 0:
            train_and_evaluate(rank, epoch, hps, [net_g, net_d], [optim_g, optim_d], [scheduler_g, scheduler_d], scaler,
                               [train_loader, eval_loader], logger, [writer, writer_eval])
        else:
            train_and_evaluate(rank, epoch, hps, [net_g, net_d], [optim_g, optim_d], [scheduler_g, scheduler_d], scaler,
                               [train_loader, None], None, None)
        scheduler_g.step()
        scheduler_d.step()


def train_and_evaluate(rank, epoch, hps, nets, optims, schedulers, scaler, loaders, logger, writers):
    net_g, net_d = nets
    optim_g, optim_d = optims
    scheduler_g, scheduler_d = schedulers
    train_loader, eval_loader = loaders
    if writers is not None:
        writer, writer_eval = writers

    train_loader.batch_sampler.set_epoch(epoch)
    global global_step

    net_g.train()
    net_d.train()
    for batch_idx, (x, x_lengths, spec, spec_lengths, y, y_lengths, pitch, speakers) in enumerate(train_loader):
        x, x_lengths = x.cuda(rank, non_blocking=True), x_lengths.cuda(rank, non_blocking=True)
        spec, spec_lengths = spec.cuda(rank, non_blocking=True), spec_lengths.cuda(rank, non_blocking=True)
        y, y_lengths = y.cuda(rank, non_blocking=True), y_lengths.cuda(rank, non_blocking=True)
        #####################################
        speakers = speakers.cuda(rank, non_blocking=True)
        #####################################
        pitch = pitch.cuda(rank, non_blocking=True)

        with autocast(enabled=hps.train.fp16_run):
            y_hat, l_length, attn, ids_slice, x_mask, z_mask, \
            (z, z_p, m_p, logs_p, m_q, logs_q) = net_g(x, x_lengths, spec, spec_lengths, pitch, speakers)

            mel = spec_to_mel_torch(
                spec,
                hps.data.filter_length,
                hps.data.n_mel_channels,
                hps.data.sampling_rate,
                hps.data.mel_fmin,
                hps.data.mel_fmax)
            y_mel = commons.slice_segments(mel, ids_slice, hps.train.segment_size // hps.data.hop_length)
            y_hat_mel = mel_spectrogram_torch(
                y_hat.squeeze(1),
                hps.data.filter_length,
                hps.data.n_mel_channels,
                hps.data.sampling_rate,
                hps.data.hop_length,
                hps.data.win_length,
                hps.data.mel_fmin,
                hps.data.mel_fmax
            )

            y = commons.slice_segments(y, ids_slice * hps.data.hop_length, hps.train.segment_size)  # slice

            # Discriminator
            y_d_hat_r, y_d_hat_g, _, _ = net_d(y, y_hat.detach())
            with autocast(enabled=False):
                loss_disc, losses_disc_r, losses_disc_g = discriminator_loss(y_d_hat_r, y_d_hat_g)
                loss_disc_all = loss_disc
        optim_d.zero_grad()
        scaler.scale(loss_disc_all).backward()
        scaler.unscale_(optim_d)
        grad_norm_d = commons.clip_grad_value_(net_d.parameters(), None)
        scaler.step(optim_d)

        with autocast(enabled=hps.train.fp16_run):
            # Generator
            y_d_hat_r, y_d_hat_g, fmap_r, fmap_g = net_d(y, y_hat)
            with autocast(enabled=False):
                loss_dur = torch.sum(l_length.float())
                loss_mel = F.l1_loss(y_mel, y_hat_mel) * hps.train.c_mel
                loss_kl = kl_loss(z_p, logs_q, m_p, logs_p, z_mask) * hps.train.c_kl

                loss_fm = feature_loss(fmap_r, fmap_g)
                loss_gen, losses_gen = generator_loss(y_d_hat_g)
                loss_gen_all = loss_gen + loss_fm + loss_mel + loss_dur + loss_kl
        optim_g.zero_grad()
        scaler.scale(loss_gen_all).backward()
        scaler.unscale_(optim_g)
        grad_norm_g = commons.clip_grad_value_(net_g.parameters(), None)
        scaler.step(optim_g)
        scaler.update()

        if rank == 0:
            if global_step % hps.train.log_interval == 0:
                lr = optim_g.param_groups[0]['lr']
                losses = [loss_disc, loss_gen, loss_fm, loss_mel, loss_dur, loss_kl]
                logger.info('Train Epoch: {} [{:.0f}%]'.format(
                    epoch,
                    100. * batch_idx / len(train_loader)))
                logger.info([x.item() for x in losses] + [global_step, lr])

                scalar_dict = {"loss/g/total": loss_gen_all, "loss/d/total": loss_disc_all, "learning_rate": lr,
                               "grad_norm_d": grad_norm_d, "grad_norm_g": grad_norm_g}
                scalar_dict.update(
                    {"loss/g/fm": loss_fm, "loss/g/mel": loss_mel, "loss/g/dur": loss_dur, "loss/g/kl": loss_kl})

                scalar_dict.update({"loss/g/{}".format(i): v for i, v in enumerate(losses_gen)})
                scalar_dict.update({"loss/d_r/{}".format(i): v for i, v in enumerate(losses_disc_r)})
                scalar_dict.update({"loss/d_g/{}".format(i): v for i, v in enumerate(losses_disc_g)})
                image_dict = {
                    "slice/mel_org": utils.plot_spectrogram_to_numpy(y_mel[0].data.cpu().numpy()),
                    "slice/mel_gen": utils.plot_spectrogram_to_numpy(y_hat_mel[0].data.cpu().numpy()),
                    "all/mel": utils.plot_spectrogram_to_numpy(mel[0].data.cpu().numpy()),
                    "all/attn": utils.plot_alignment_to_numpy(attn[0, 0].data.cpu().numpy())
                }
                utils.summarize(
                    writer=writer,
                    global_step=global_step,
                    images=image_dict,
                    scalars=scalar_dict)

            if global_step % hps.train.eval_interval == 0:
                evaluate(hps, net_g, eval_loader, writer_eval)
                utils.save_checkpoint(net_g, optim_g, hps.train.learning_rate, epoch,
                                      os.path.join(hps.model_dir, "G_{}.pth".format(global_step)))
                utils.save_checkpoint(net_d, optim_d, hps.train.learning_rate, epoch,
                                      os.path.join(hps.model_dir, "D_{}.pth".format(global_step)))
        global_step += 1

    if rank == 0:
        logger.info('====> Epoch: {}'.format(epoch))


def evaluate(hps, generator, eval_loader, writer_eval):
    generator.eval()
    with torch.no_grad():
        for batch_idx, (x, x_lengths, spec, spec_lengths, y, y_lengths, pitch, speakers) in enumerate(eval_loader):
            x, x_lengths = x.cuda(0), x_lengths.cuda(0)
            spec, spec_lengths = spec.cuda(0), spec_lengths.cuda(0)
            y, y_lengths = y.cuda(0), y_lengths.cuda(0)
            ###############################
            speakers = speakers.cuda(0)
            ###############################
            pitch = pitch.cuda(0)
            # remove else
            x = x[:1]
            x_lengths = x_lengths[:1]
            spec = spec[:1]
            spec_lengths = spec_lengths[:1]
            y = y[:1]
            y_lengths = y_lengths[:1]
            ###############################
            speakers = speakers[:1]
            ###############################
            break
        y_hat, attn, mask, *_ = generator.module.infer(x, x_lengths, pitch, speakers, max_len=1000)
        y_hat_lengths = mask.sum([1, 2]).long() * hps.data.hop_length

        mel = spec_to_mel_torch(
            spec,
            hps.data.filter_length,
            hps.data.n_mel_channels,
            hps.data.sampling_rate,
            hps.data.mel_fmin,
            hps.data.mel_fmax)
        y_hat_mel = mel_spectrogram_torch(
            y_hat.squeeze(1).float(),
            hps.data.filter_length,
            hps.data.n_mel_channels,
            hps.data.sampling_rate,
            hps.data.hop_length,
            hps.data.win_length,
            hps.data.mel_fmin,
            hps.data.mel_fmax
        )
    image_dict = {
        "gen/mel": utils.plot_spectrogram_to_numpy(y_hat_mel[0].cpu().numpy())
    }
    audio_dict = {
        "gen/audio": y_hat[0, :, :y_hat_lengths[0]]
    }
    if global_step == 0:
        image_dict.update({"gt/mel": utils.plot_spectrogram_to_numpy(mel[0].cpu().numpy())})
        audio_dict.update({"gt/audio": y[0, :, :y_lengths[0]]})

    utils.summarize(
        writer=writer_eval,
        global_step=global_step,
        images=image_dict,
        audios=audio_dict,
        audio_sampling_rate=hps.data.sampling_rate
    )
    generator.train()


if __name__ == "__main__":
    main()
