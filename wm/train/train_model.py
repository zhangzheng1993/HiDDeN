import time
import os
import numpy as np
from collections import defaultdict

import logging
import torch
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from model.watermarker_base import WatermarkerBase
from model.hidden.hidden_model import Hidden
from model.unet.unet_model import UnetModel
from train.loss_names import LossNames
from train.tensorboard_logger import TensorBoardLogger
from train.average_meter import AverageMeter
import util.common as common


def train(model: WatermarkerBase,
        #   device: torch.device,
          job_name: str,
          job_folder: str,
          image_size: int, 
          train_folder: str,
          validation_folder: str, 
          batch_size: int,
          number_of_epochs: int,
          message_length: int,
          start_epoch: int, 
          tb_writer: SummaryWriter=None, 
          checkpoint_folder: str=None):
    """
    Trains a watermark embedding-extracting model
    :param model: The model
    :param device: torch.device object, usually this is GPU (if avaliable), otherwise CPU.
    :param train_options: The training settings
    :param this_run_folder: The parent folder for the current training run to store training artifacts/results/logs.
    :param tb_logger: TensorBoardLogger object which is a thin wrapper for TensorboardX logger.
    :return:
    """

    train_data, val_data = common.get_data_loaders(image_height=image_size, image_width=image_size, 
                        train_folder=train_folder, validation_folder=validation_folder, batch_size=batch_size)
    print(f'Loaded train and validation data loaders')
    file_count = len(train_data.dataset)
    if file_count % batch_size == 0:
        steps_in_epoch = file_count // batch_size
    else:
        steps_in_epoch = file_count // batch_size + 1

    image_save_epochs = 10
    images_to_save = 8
    saved_images_size = (512, 512)
    if checkpoint_folder is None:
        checkpoint_folder = os.path.join(job_folder, 'checkpoints')
    best_validation_error = np.inf

    # tqdm_format = '{desc}: {percentage:3.0f}%|{bar}| [ {postfix}]'
    tqdm_format = '{desc}{percentage:3.0f}%|{bar}|'
    # pbar_postfix = 'enc: {enc_mse:.7} bit: {bit_error:.7} discrim: {d_bce:.5}'
    pbar_desc = '{step:' + str(len(str(steps_in_epoch))) + '}/{total_steps} ' + \
                'enc: {enc_mse:5.4f} bit: {bit_error:5.4f} discrim: {d_bce:5.4f}'

    pbar = tqdm(position=0, bar_format=tqdm_format, total=steps_in_epoch, leave=True, dynamic_ncols=True)
    for epoch in range(start_epoch, number_of_epochs + 1):
        tqdm.write(f'Epoch: {epoch:3}/{number_of_epochs}')
        pbar.reset()
        training_losses = defaultdict(AverageMeter)
        epoch_start = time.time()
        step = 1    
        for image, _ in train_data:
            image = image.to(model.device)
            message = torch.Tensor(np.random.choice([0, 1], (image.shape[0], message_length))).to(model.device)
            losses, _ = model.train_on_batch(image, message)

            for name, loss in losses.items():
                training_losses[name].update(loss)
            pbar.update()
            pbar.set_description(pbar_desc.format(
                epoch = epoch,
                total_epochs = number_of_epochs,
                step = step,
                total_steps = steps_in_epoch,
                enc_mse=training_losses[LossNames.encoder_mse.value].avg,
                bit_error=training_losses[LossNames.bitwise.value].avg, 
                d_bce=(training_losses[LossNames.discr_cov_bce.value].avg+training_losses[LossNames.discr_enc_bce.value].avg)/2
            ))
            step += 1
            # if tb_writer:
            #     # tensorboard logging is enabled, log last run stats
            #     encoder_grads = model.encoder_decoder.encoder.get_tensors_for_logging()
            #     decoder_grads = model.encoder_decoder.decoder.get_grads_for_logging(gradients=True)
            #     discrim_grads = model.discriminator.data_for_logging(gradients=True)
            #     for key in encoder_grads:
            #         tb_writer.add_histogram(tag='grads/encoder/{key}', values=encoder_grads[key], global_step=epoch)
            #     for key in decoder_grads:
            #         tb_writer.add_histogram(tag='grads/decoder/{key}', values=decoder_grads[key], global_step=epoch)
            #     for key in discrim_grads:
            #         tb_writer.add_histogram(tag='grads/discrim/{key}', values=discrim_grads[key], global_step=epoch)


        train_duration = time.time() - epoch_start
        logging.info('Epoch {} training duration {:.2f} sec'.format(epoch, train_duration))
        logging.info('-' * 40)
        common.write_losses(os.path.join(job_folder, 'train.csv'), training_losses, epoch, train_duration)

        # if model.tb_logger is not None:
        #     # create a dummy loss variable 
        #     losses_to_save = {
        #         f'train/{LossNames.bitwise.value}': training_losses[LossNames.bitwise.value].avg,
        #         f'train/{LossNames.encoder_mse.value}': training_losses[LossNames.encoder_mse.value].avg,
        #         f'train/discrim_bce': (training_losses[LossNames.discr_cov_bce.value].avg+training_losses[LossNames.discr_enc_bce.value].avg)/2
        #     }
        #     # model.tb_logger.save_losses(losses_to_save, epoch)
        #     # model.tb_logger.save_grads(epoch)
        #     # model.tb_logger.save_tensors(epoch)

        if tb_writer:
            losses_to_save = {
                LossNames.bitwise.value: training_losses[LossNames.bitwise.value].avg,
                LossNames.encoder_mse.value: training_losses[LossNames.encoder_mse.value].avg,
                'discrim_bce': (training_losses[LossNames.discr_cov_bce.value].avg+training_losses[LossNames.discr_enc_bce.value].avg)/2
            }
            for loss_name in losses_to_save:
                tb_writer.add_scalar(tag=f'train/{loss_name}', scalar_value=losses_to_save[loss_name], global_step=epoch)

            
        first_iteration = True
        validation_losses = defaultdict(AverageMeter)

        logging.info('Running validation for epoch {}/{}'.format(epoch, number_of_epochs))
        for image, _ in val_data:
            image = image.to(model.device)
            message = torch.Tensor(np.random.choice([0, 1], (image.shape[0], message_length))).to(model.device)
            losses, (encoded_images, noised_images, decoded_messages) = model.validate_on_batch(image, message)
            for name, loss in losses.items():
                validation_losses[name].update(loss)
            if first_iteration and epoch % image_save_epochs == 0:
                # if model.net_config.enable_fp16:
                #     image = image.float()
                #     encoded_images = encoded_images.float()
                cover_cpu = (image[:images_to_save, :, :, :].cpu() + 1)/2
                encoded_cpu = (encoded_images[:images_to_save, :, :, :].cpu() + 1)/2
                common.save_images(cover_images=cover_cpu,
                                  processed_images=encoded_cpu,
                                  filename=os.path.join(job_folder, 'images', f'epoch-{epoch}.png'), 
                                  resize_to=saved_images_size)
                if tb_writer:
                    common.save_to_tensorboard(cover_images=cover_cpu, encoded_images=encoded_cpu, tb_writer=tb_writer, global_step=epoch)
                first_iteration = False
                # if tb_writer: 
                #     encoder_tensors = model.encoder_decoder.encoder.tensors_for_logging()
                #     decoder_tensors = model.encoder_decoder.decoder.tensors_for_logging()
                #     discrim_tensors = model.discriminator.tensors_for_logging()
                #     for key in encoder_tensors:
                #         tb_writer.add_histogram(tag='tensors/encoder/{key}', values=encoder_tensors[key], global_step=epoch)
                #     for key in decoder_tensors:
                #         tb_writer.add_histogram(tag='tensors/decoder/{key}', values=decoder_tensors[key], global_step=epoch)
                #     for key in discrim_tensors:
                #         tb_writer.add_histogram(tag='tensors/decoder/{key}', values=discrim_tensors[key], global_step=epoch)

        logging.info(common.losses_to_string(validation_losses))
        logging.info('-' * 40)
        common.update_checkpoint(model, job_name, epoch, checkpoint_folder, 'last')

        if isinstance(model, Hidden):
            network_loss = validation_losses[LossNames.hidden_loss.value].avg
        elif isinstance(model, UnetModel):
            network_loss = validation_losses[LossNames.unet_loss.value].avg
        else:
            raise ValueError('Only "hidden" or "unet" networks are supported')

        if network_loss < best_validation_error:
            common.update_checkpoint(model, job_name, epoch, checkpoint_folder, 'best')
            best_validation_error = network_loss

        common.write_losses(os.path.join(job_folder, 'validation.csv'), validation_losses, epoch,
                           time.time() - epoch_start)
        # if model.tb_logger is not None:
        #     # create a dummy loss variable 
        #     losses_to_save = {
        #         f'validation/{LossNames.bitwise.value}': validation_losses[LossNames.bitwise.value].avg,
        #         f'validation/{LossNames.encoder_mse.value}': validation_losses[LossNames.encoder_mse.value].avg,
        #         f'validation/discrim_bce': (validation_losses[LossNames.discr_cov_bce.value].avg+training_losses[LossNames.discr_enc_bce.value].avg)/2
        #     }
        #     model.tb_logger.save_losses(losses_to_save, epoch)
        #     model.tb_logger.save_grads(epoch)
        #     model.tb_logger.save_tensors(epoch)
        if tb_writer:
            losses_to_save = {
                LossNames.bitwise.value: validation_losses[LossNames.bitwise.value].avg,
                LossNames.encoder_mse.value: validation_losses[LossNames.encoder_mse.value].avg,
                'discrim_bce': (validation_losses[LossNames.discr_cov_bce.value].avg+training_losses[LossNames.discr_enc_bce.value].avg)/2
            }
            for loss_name in losses_to_save:
                tb_writer.add_scalar(tag=f'validation/{loss_name}', scalar_value=losses_to_save[loss_name], global_step=epoch)
            