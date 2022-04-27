# Copyright (C) 2021. Huawei Technologies Co., Ltd. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This file contains code fragments from 
#
#     ../ctc_bpe_models.py
#     ../ctc_models.py
#
# with the following copyright statement.
#
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import contextlib
import copy
import itertools
import json
import os
import tempfile
from math import ceil
from typing import Dict, List, Optional, Union

import torch
from omegaconf import DictConfig, OmegaConf, open_dict, ListConfig
from pytorch_lightning import Trainer
from tqdm.auto import tqdm

from nemo.collections.asr.losses.rnnt import RNNTLoss, resolve_rnnt_default_loss_name
from nemo.collections.asr.metrics.rnnt_wer import RNNTWER, RNNTDecoding, RNNTDecodingConfig
from spiral_nemo.utils import logging
from spiral_nemo.collections.asr.models.spec2vec.ctc_finetune import CTCFinetuneModel
from nemo.collections.asr.metrics.rnnt_wer import RNNTWER


from nemo.collections.asr.data.audio_to_text_dali import DALIOutputs
from nemo.collections.asr.losses.rnnt import RNNTLoss, resolve_rnnt_default_loss_name
from nemo.collections.asr.metrics.rnnt_wer import RNNTWER, RNNTDecoding, RNNTDecodingConfig
from nemo.collections.asr.modules.rnnt import RNNTDecoderJoint
from nemo.collections.asr.parts.preprocessing.perturb import process_augmentations
from nemo.core.classes.common import PretrainedModelInfo, typecheck
from nemo.utils import logging
from nemo.utils.export_utils import augment_filename
import IPython
import copy
import json
import os
import tempfile
from math import ceil
from typing import Dict, List, Optional, Union

import torch
from omegaconf import DictConfig, OmegaConf, open_dict
from pytorch_lightning import Trainer
from tqdm.auto import tqdm


class RNNTFinetuneModel(CTCFinetuneModel):
    """Base class for encoder decoder RNNT-based models."""

    @classmethod
    def list_available_models(cls) -> Optional[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.

        Returns:
            List of available pre-trained models.
        """
        result = []
        return result

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        # Get global rank and total number of GPU workers for IterableDataset partitioning, if applicable
        # Global_rank and local_rank is set by LightningModule in Lightning 1.2.0
        self.world_size = 1
        self.use_bpe = False
        if trainer is not None:
            self.world_size = trainer.world_size
        self.add_end_space = cfg.add_end_space
        super(type(self).__bases__[0], self).__init__(cfg=cfg, trainer=trainer)

        # Initialize components
        assert self._cfg.encoder_type == 'st'
        from spiral_nemo.collections.asr.models.st2vec.st2vec_model import ST2VecEncoder
        self.encoder = ST2VecEncoder(self._cfg.encoder)
        encoder_param_prefix = 'st2vec_encoder.'
        if cfg.pretrain_chkpt_path is not None:
            RNNTFinetuneModel.init_encoder_from_pretrain_model(self.encoder, encoder_param_prefix, cfg.pretrain_chkpt_path)
        if self._cfg.encoder_type == 'st':
            self.encoder.remove_pretraining_modules(use_teacher_encoder=self._cfg.use_teacher_encoder)
        else:
            self.encoder.remove_pretraining_modules()
        # Update config values required by components dynamically
        with open_dict(self.cfg.decoder):
            self.cfg.decoder.vocab_size = len(self.cfg.labels)

        with open_dict(self.cfg.joint):
            self.cfg.joint.num_classes = len(self.cfg.labels)
            self.cfg.joint.vocabulary = self.cfg.labels
            self.cfg.joint.jointnet.encoder_hidden = self.cfg.model_defaults.enc_hidden
            self.cfg.joint.jointnet.pred_hidden = self.cfg.model_defaults.pred_hidden

        self.decoder = RNNTFinetuneModel.from_config_dict(self.cfg.decoder)
        self.joint = RNNTFinetuneModel.from_config_dict(self.cfg.joint)

        self.freeze_finetune_updates = self._cfg.freeze_finetune_updates
        # Setup RNNT Loss
        loss_name, loss_kwargs = self.extract_rnnt_loss_cfg(self.cfg.get("loss", None))

        self.loss = RNNTLoss(
            num_classes=self.joint.num_classes_with_blank - 1, loss_name=loss_name, loss_kwargs=loss_kwargs
        )

        if hasattr(self.cfg, 'spec_augment') and self._cfg.spec_augment is not None:
            self.spec_augmentation = RNNTFinetuneModel.from_config_dict(self.cfg.spec_augment)
        else:
            self.spec_augmentation = None

        # Setup decoding objects
        self.decoding = RNNTDecoding(
            decoding_cfg=self.cfg.decoding, decoder=self.decoder, joint=self.joint, vocabulary=self.joint.vocabulary,
        )
        # Setup WER calculation
        self.wer = RNNTWER(
            decoding=self.decoding,
            batch_dim_index=0,
            use_cer=self._cfg.get('use_cer', False),
            log_prediction=self._cfg.get('log_prediction', True),
            dist_sync_on_step=True,
        )

        # Whether to compute loss during evaluation
        if 'compute_eval_loss' in self.cfg:
            self.compute_eval_loss = self.cfg.compute_eval_loss
        else:
            self.compute_eval_loss = True

        # Setup fused Joint step if flag is set
        if self.joint.fuse_loss_wer or (
            self.decoding.joint_fused_batch_size is not None and self.decoding.joint_fused_batch_size > 0
        ):
            self.joint.set_loss(self.loss)
            self.joint.set_wer(self.wer)

        self.setup_optim_normalization()

        # Setup optional Optimization flags
        self.setup_optimization_flags()

    def setup_optimization_flags(self):
        """
        Utility method that must be explicitly called by the subclass in order to support optional optimization flags.
        This method is the only valid place to access self.cfg prior to DDP training occurs.

        The subclass may chose not to support this method, therefore all variables here must be checked via hasattr()
        """
        # Skip update if nan/inf grads appear on any rank.
        self._skip_nan_grad = False
        if "skip_nan_grad" in self._cfg and self._cfg["skip_nan_grad"]:
            self._skip_nan_grad = self._cfg["skip_nan_grad"]
    
    def setup_optim_normalization(self):
        """
        Helper method to setup normalization of certain parts of the model prior to the optimization step.

        Supported pre-optimization normalizations are as follows:

        .. code-block:: yaml

            # Variation Noise injection
            model:
                variational_noise:
                    std: 0.0
                    start_step: 0

            # Joint - Length normalization
            model:
                normalize_joint_txu: false

            # Encoder Network - gradient normalization
            model:
                normalize_encoder_norm: false

            # Decoder / Prediction Network - gradient normalization
            model:
                normalize_decoder_norm: false

            # Joint - gradient normalization
            model:
                normalize_joint_norm: false
        """
        # setting up the variational noise for the decoder
        if hasattr(self.cfg, 'variational_noise'):
            self._optim_variational_noise_std = self.cfg['variational_noise'].get('std', 0)
            self._optim_variational_noise_start = self.cfg['variational_noise'].get('start_step', 0)
        else:
            self._optim_variational_noise_std = 0
            self._optim_variational_noise_start = 0

        # Setup normalized gradients for model joint by T x U scaling factor (joint length normalization)
        self._optim_normalize_joint_txu = self.cfg.get('normalize_joint_txu', False)
        self._optim_normalize_txu = None

        # Setup normalized encoder norm for model
        self._optim_normalize_encoder_norm = self.cfg.get('normalize_encoder_norm', False)

        # Setup normalized decoder norm for model
        self._optim_normalize_decoder_norm = self.cfg.get('normalize_decoder_norm', False)

        # Setup normalized joint norm for model
        self._optim_normalize_joint_norm = self.cfg.get('normalize_joint_norm', False)

    def extract_rnnt_loss_cfg(self, cfg: Optional[DictConfig]):
        """
        Helper method to extract the rnnt loss name, and potentially its kwargs
        to be passed.

        Args:
            cfg: Should contain `loss_name` as a string which is resolved to a RNNT loss name.
                If the default should be used, then `default` can be used.
                Optionally, one can pass additional kwargs to the loss function. The subdict
                should have a keyname as follows : `{loss_name}_kwargs`.

                Note that whichever loss_name is selected, that corresponding kwargs will be
                selected. For the "default" case, the "{resolved_default}_kwargs" will be used.

        Examples:
            .. code-block:: yaml

                loss_name: "default"
                warprnnt_numba_kwargs:
                    kwargs2: some_other_val

        Returns:
            A tuple, the resolved loss name as well as its kwargs (if found).
        """
        if cfg is None:
            cfg = DictConfig({})

        loss_name = cfg.get("loss_name", "default")

        if loss_name == "default":
            loss_name = resolve_rnnt_default_loss_name()

        loss_kwargs = cfg.get(f"{loss_name}_kwargs", None)

        logging.info(f"Using RNNT Loss : {loss_name}\n" f"Loss {loss_name}_kwargs: {loss_kwargs}")

        return loss_name, loss_kwargs

    @torch.no_grad()
    def transcribe(
        self,
        paths2audio_files: List[str],
        batch_size: int = 4,
        return_hypotheses: bool = False,
        partial_hypothesis: Optional[List['Hypothesis']] = None,
        num_workers: int = 0,
    ) -> (List[str], Optional[List['Hypothesis']]):
        """
        Uses greedy decoding to transcribe audio files. Use this method for debugging and prototyping.

        Args:

            paths2audio_files: (a list) of paths to audio files. \
        Recommended length per file is between 5 and 25 seconds. \
        But it is possible to pass a few hours long file if enough GPU memory is available.
            batch_size: (int) batch size to use during inference. \
        Bigger will result in better throughput performance but would use more memory.
            return_hypotheses: (bool) Either return hypotheses or text
        With hypotheses can do some postprocessing like getting timestamp or rescoring
            num_workers: (int) number of workers for DataLoader

        Returns:
            A list of transcriptions in the same order as paths2audio_files. Will also return
        """
        if paths2audio_files is None or len(paths2audio_files) == 0:
            return {}
        # We will store transcriptions here
        hypotheses = []
        all_hypotheses = []
        # Model's mode and device
        mode = self.training
        device = next(self.parameters()).device
        dither_value = self.preprocessor.featurizer.dither
        pad_to_value = self.preprocessor.featurizer.pad_to

        if num_workers is None:
            num_workers = min(batch_size, os.cpu_count() - 1)

        try:
            self.preprocessor.featurizer.dither = 0.0
            self.preprocessor.featurizer.pad_to = 0

            # Switch model to evaluation mode
            self.eval()
            # Freeze the encoder and decoder modules
            self.encoder.freeze()
            self.decoder.freeze()
            self.joint.freeze()
            logging_level = logging.get_verbosity()
            logging.set_verbosity(logging.WARNING)
            # Work in tmp directory - will store manifest file there
            with tempfile.TemporaryDirectory() as tmpdir:
                with open(os.path.join(tmpdir, 'manifest.json'), 'w', encoding='utf-8') as fp:
                    for audio_file in paths2audio_files:
                        entry = {'audio_filepath': audio_file, 'duration': 100000, 'text': ''}
                        fp.write(json.dumps(entry) + '\n')

                config = {
                    'paths2audio_files': paths2audio_files,
                    'batch_size': batch_size,
                    'temp_dir': tmpdir,
                    'num_workers': num_workers,
                }

                temporary_datalayer = self._setup_transcribe_dataloader(config)
                for test_batch in tqdm(temporary_datalayer, desc="Transcribing"):
                    encoded, encoded_len = self.forward(
                        input_signal=test_batch[0].to(device), input_signal_length=test_batch[1].to(device)
                    )
                    best_hyp, all_hyp = self.decoding.rnnt_decoder_predictions_tensor(
                        encoded,
                        encoded_len,
                        return_hypotheses=return_hypotheses,
                        partial_hypotheses=partial_hypothesis,
                    )

                    hypotheses += best_hyp
                    if all_hyp is not None:
                        all_hypotheses += all_hyp
                    else:
                        all_hypotheses += best_hyp

                    del encoded
                    del test_batch
        finally:
            # set mode back to its original value
            self.train(mode=mode)
            self.preprocessor.featurizer.dither = dither_value
            self.preprocessor.featurizer.pad_to = pad_to_value

            logging.set_verbosity(logging_level)
            if mode is True:
                self.encoder.unfreeze()
                self.decoder.unfreeze()
                self.joint.unfreeze()
        return hypotheses, all_hypotheses

    def change_vocabulary(self, new_vocabulary: List[str], decoding_cfg: Optional[DictConfig] = None):
        """
        Changes vocabulary used during RNNT decoding process. Use this method when fine-tuning a pre-trained model.
        This method changes only decoder and leaves encoder and pre-processing modules unchanged. For example, you would
        use it if you want to use pretrained encoder when fine-tuning on data in another language, or when you'd need
        model to learn capitalization, punctuation and/or special characters.

        Args:
            new_vocabulary: list with new vocabulary. Must contain at least 2 elements. Typically, \
                this is target alphabet.
            decoding_cfg: A config for the decoder, which is optional. If the decoding type
                needs to be changed (from say Greedy to Beam decoding etc), the config can be passed here.

        Returns: None

        """
        if self.joint.vocabulary == new_vocabulary:
            logging.warning(f"Old {self.joint.vocabulary} and new {new_vocabulary} match. Not changing anything.")
        else:
            if new_vocabulary is None or len(new_vocabulary) == 0:
                raise ValueError(f'New vocabulary must be non-empty list of chars. But I got: {new_vocabulary}')

            joint_config = self.joint.to_config_dict()
            new_joint_config = copy.deepcopy(joint_config)
            new_joint_config['vocabulary'] = new_vocabulary
            new_joint_config['num_classes'] = len(new_vocabulary)
            del self.joint
            self.joint = EncDecRNNTModel.from_config_dict(new_joint_config)

            decoder_config = self.decoder.to_config_dict()
            new_decoder_config = copy.deepcopy(decoder_config)
            new_decoder_config.vocab_size = len(new_vocabulary)
            del self.decoder
            self.decoder = EncDecRNNTModel.from_config_dict(new_decoder_config)

            del self.loss
            loss_name, loss_kwargs = self.extract_rnnt_loss_cfg(self.cfg.get('loss', None))
            self.loss = RNNTLoss(
                num_classes=self.joint.num_classes_with_blank - 1, loss_name=loss_name, loss_kwargs=loss_kwargs
            )

            if decoding_cfg is None:
                # Assume same decoding config as before
                decoding_cfg = self.cfg.decoding

            # Assert the decoding config with all hyper parameters
            decoding_cls = OmegaConf.structured(RNNTDecodingConfig)
            decoding_cls = OmegaConf.create(OmegaConf.to_container(decoding_cls))
            decoding_cfg = OmegaConf.merge(decoding_cls, decoding_cfg)

            self.decoding = RNNTDecoding(
                decoding_cfg=decoding_cfg, decoder=self.decoder, joint=self.joint, vocabulary=self.joint.vocabulary,
            )

            self.wer = RNNTWER(
                decoding=self.decoding,
                batch_dim_index=self.wer.batch_dim_index,
                use_cer=self.wer.use_cer,
                log_prediction=self.wer.log_prediction,
                dist_sync_on_step=True,
            )

            # Setup fused Joint step
            if self.joint.fuse_loss_wer or (
                self.decoding.joint_fused_batch_size is not None and self.decoding.joint_fused_batch_size > 0
            ):
                self.joint.set_loss(self.loss)
                self.joint.set_wer(self.wer)

            # Update config
            with open_dict(self.cfg.joint):
                self.cfg.joint = new_joint_config

            with open_dict(self.cfg.decoder):
                self.cfg.decoder = new_decoder_config

            with open_dict(self.cfg.decoding):
                self.cfg.decoding = decoding_cfg

            ds_keys = ['train_ds', 'validation_ds', 'test_ds']
            for key in ds_keys:
                if key in self.cfg:
                    with open_dict(self.cfg[key]):
                        self.cfg[key]['labels'] = OmegaConf.create(new_vocabulary)

            logging.info(f"Changed decoder to output to {self.joint.vocabulary} vocabulary.")

    def change_decoding_strategy(self, decoding_cfg: DictConfig):
        """
        Changes decoding strategy used during RNNT decoding process.

        Args:
            decoding_cfg: A config for the decoder, which is optional. If the decoding type
                needs to be changed (from say Greedy to Beam decoding etc), the config can be passed here.
        """
        if decoding_cfg is None:
            # Assume same decoding config as before
            logging.info("No `decoding_cfg` passed when changing decoding strategy, using internal config")
            decoding_cfg = self.cfg.decoding

        # Assert the decoding config with all hyper parameters
        decoding_cls = OmegaConf.structured(RNNTDecodingConfig)
        decoding_cls = OmegaConf.create(OmegaConf.to_container(decoding_cls))
        decoding_cfg = OmegaConf.merge(decoding_cls, decoding_cfg)

        self.decoding = RNNTDecoding(
            decoding_cfg=decoding_cfg, decoder=self.decoder, joint=self.joint, vocabulary=self.joint.vocabulary,
        )

        self.wer = RNNTWER(
            decoding=self.decoding,
            batch_dim_index=self.wer.batch_dim_index,
            use_cer=self.wer.use_cer,
            log_prediction=self.wer.log_prediction,
            dist_sync_on_step=True,
        )

        # Setup fused Joint step
        if self.joint.fuse_loss_wer or (
            self.decoding.joint_fused_batch_size is not None and self.decoding.joint_fused_batch_size > 0
        ):
            self.joint.set_loss(self.loss)
            self.joint.set_wer(self.wer)

        # Update config
        with open_dict(self.cfg.decoding):
            self.cfg.decoding = decoding_cfg

        logging.info(f"Changed decoding strategy to \n{OmegaConf.to_yaml(self.cfg.decoding)}")

    @typecheck()
    def forward(
        self, input_signal=None, input_signal_length=None, processed_signal=None, processed_signal_length=None
    ):
        """
        Forward pass of the model. Note that for RNNT Models, the forward pass of the model is a 3 step process,
        and this method only performs the first step - forward of the acoustic model.

        Please refer to the `training_step` in order to see the full `forward` step for training - which
        performs the forward of the acoustic model, the prediction network and then the joint network.
        Finally, it computes the loss and possibly compute the detokenized text via the `decoding` step.

        Please refer to the `validation_step` in order to see the full `forward` step for inference - which
        performs the forward of the acoustic model, the prediction network and then the joint network.
        Finally, it computes the decoded tokens via the `decoding` step and possibly compute the batch metrics.

        Args:
            input_signal: Tensor that represents a batch of raw audio signals,
                of shape [B, T]. T here represents timesteps, with 1 second of audio represented as
                `self.sample_rate` number of floating point values.
            input_signal_length: Vector of length B, that contains the individual lengths of the audio
                sequences.
            processed_signal: Tensor that represents a batch of processed audio signals,
                of shape (B, D, T) that has undergone processing via some DALI preprocessor.
            processed_signal_length: Vector of length B, that contains the individual lengths of the
                processed audio sequences.

        Returns:
            A tuple of 2 elements -
            1) The log probabilities tensor of shape [B, T, D].
            2) The lengths of the acoustic sequence after propagation through the encoder, of shape [B].
        """
        has_input_signal = input_signal is not None and input_signal_length is not None
        has_processed_signal = processed_signal is not None and processed_signal_length is not None
        if (has_input_signal ^ has_processed_signal) is False:
            raise ValueError(
                f"{self} Arguments ``input_signal`` and ``input_signal_length`` are mutually exclusive "
                " with ``processed_signal`` and ``processed_signal_len`` arguments."
            )

        if not has_processed_signal:
            processed_signal, processed_signal_length = self.preprocessor(
                input_signal=input_signal, length=input_signal_length,
            )

        # Spec augment is not applied during evaluation/testing
        if self.spec_augmentation is not None and self.training:
            processed_signal = self.spec_augmentation(input_spec=processed_signal, length=processed_signal_length)

        encoded, encoded_len = self.encoder(audio_signal=processed_signal, length=processed_signal_length)
        return encoded, encoded_len

    # PTL-specific methods
    def training_step(self, batch, batch_nb):
        signal, signal_len, transcript, transcript_len = batch

        # forward() only performs encoder forward
        if isinstance(batch, DALIOutputs) and batch.has_processed_signal:
            encoded, encoded_len = self.forward(processed_signal=signal, processed_signal_length=signal_len)
        else:
            encoded, encoded_len = self.forward(input_signal=signal, input_signal_length=signal_len)
        del signal

        # During training, loss must be computed, so decoder forward is necessary
        decoder, target_length, states = self.decoder(targets=transcript, target_length=transcript_len)

        if hasattr(self, '_trainer') and self._trainer is not None:
            log_every_n_steps = self._trainer.log_every_n_steps
            sample_id = self._trainer.global_step
        else:
            log_every_n_steps = 1
            sample_id = batch_nb

        # If experimental fused Joint-Loss-WER is not used
        if not self.joint.fuse_loss_wer:
            # Compute full joint and loss
            joint = self.joint(encoder_outputs=encoded, decoder_outputs=decoder)
            loss_value = self.loss(
                log_probs=joint, targets=transcript, input_lengths=encoded_len, target_lengths=target_length
            )

            tensorboard_logs = {'train_loss': loss_value, 'learning_rate': self._optimizer.param_groups[0]['lr']}

            if (sample_id + 1) % log_every_n_steps == 0:
                self.wer.update(encoded, encoded_len, transcript, transcript_len)
                _, scores, words = self.wer.compute()
                self.wer.reset()
                tensorboard_logs.update({'training_batch_wer': scores.float() / words})

        else:
            # If experimental fused Joint-Loss-WER is used
            if (sample_id + 1) % log_every_n_steps == 0:
                compute_wer = True
            else:
                compute_wer = False

            # Fused joint step
            loss_value, wer, _, _ = self.joint(
                encoder_outputs=encoded,
                decoder_outputs=decoder,
                encoder_lengths=encoded_len,
                transcripts=transcript,
                transcript_lengths=transcript_len,
                compute_wer=compute_wer,
            )

            tensorboard_logs = {'train_loss': loss_value, 'learning_rate': self._optimizer.param_groups[0]['lr']}

            if compute_wer:
                tensorboard_logs.update({'training_batch_wer': wer})

        # Log items
        self.log_dict(tensorboard_logs)

        # Preserve batch acoustic model T and language model U parameters if normalizing
        if self._optim_normalize_joint_txu:
            self._optim_normalize_txu = [encoded_len.max(), transcript_len.max()]

        return {'loss': loss_value}

    def predict_step(self, batch, batch_idx, dataloader_idx=0):
        signal, signal_len, transcript, transcript_len, sample_id = batch

        # forward() only performs encoder forward
        if isinstance(batch, DALIOutputs) and batch.has_processed_signal:
            encoded, encoded_len = self.forward(processed_signal=signal, processed_signal_length=signal_len)
        else:
            encoded, encoded_len = self.forward(input_signal=signal, input_signal_length=signal_len)
        del signal

        best_hyp_text, all_hyp_text = self.decoding.rnnt_decoder_predictions_tensor(
            encoder_output=encoded, encoded_lengths=encoded_len, return_hypotheses=False
        )

        sample_id = sample_id.cpu().detach().numpy()
        return list(zip(sample_id, best_hyp_text))

    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        signal, signal_len, transcript, transcript_len = batch

        # forward() only performs encoder forward
        if isinstance(batch, DALIOutputs) and batch.has_processed_signal:
            encoded, encoded_len = self.forward(processed_signal=signal, processed_signal_length=signal_len)
        else:
            encoded, encoded_len = self.forward(input_signal=signal, input_signal_length=signal_len)
        del signal

        tensorboard_logs = {}

        # If experimental fused Joint-Loss-WER is not used
        if not self.joint.fuse_loss_wer:
            if self.compute_eval_loss:
                decoder, target_length, states = self.decoder(targets=transcript, target_length=transcript_len)
                joint = self.joint(encoder_outputs=encoded, decoder_outputs=decoder)

                loss_value = self.loss(
                    log_probs=joint, targets=transcript, input_lengths=encoded_len, target_lengths=target_length
                )

                tensorboard_logs['val_loss'] = loss_value

            self.wer.update(encoded, encoded_len, transcript, transcript_len)
            wer, wer_num, wer_denom = self.wer.compute()
            self.wer.reset()

            tensorboard_logs['val_wer_num'] = wer_num
            tensorboard_logs['val_wer_denom'] = wer_denom
            tensorboard_logs['val_wer'] = wer

        else:
            # If experimental fused Joint-Loss-WER is used
            compute_wer = True

            if self.compute_eval_loss:
                decoded, target_len, states = self.decoder(targets=transcript, target_length=transcript_len)
            else:
                decoded = None
                target_len = transcript_len

            # Fused joint step
            loss_value, wer, wer_num, wer_denom = self.joint(
                encoder_outputs=encoded,
                decoder_outputs=decoded,
                encoder_lengths=encoded_len,
                transcripts=transcript,
                transcript_lengths=target_len,
                compute_wer=compute_wer,
            )

            if loss_value is not None:
                tensorboard_logs['val_loss'] = loss_value

            tensorboard_logs['val_wer_num'] = wer_num
            tensorboard_logs['val_wer_denom'] = wer_denom
            tensorboard_logs['val_wer'] = wer

        return tensorboard_logs

    def test_step(self, batch, batch_idx, dataloader_idx=0):
        logs = self.validation_step(batch, batch_idx, dataloader_idx=dataloader_idx)
        test_logs = {
            'test_wer_num': logs['val_wer_num'],
            'test_wer_denom': logs['val_wer_denom'],
            # 'test_wer': logs['val_wer'],
        }
        if 'val_loss' in logs:
            test_logs['test_loss'] = logs['val_loss']
        return test_logs

    def multi_validation_epoch_end(self, outputs, dataloader_idx: int = 0):
        if self.compute_eval_loss:
            val_loss_mean = torch.stack([x['val_loss'] for x in outputs]).mean()
            val_loss_log = {'val_loss': val_loss_mean}
        else:
            val_loss_log = {}
        wer_num = torch.stack([x['val_wer_num'] for x in outputs]).sum()
        wer_denom = torch.stack([x['val_wer_denom'] for x in outputs]).sum()
        tensorboard_logs = {**val_loss_log, 'val_wer': wer_num.float() / wer_denom}
        return {**val_loss_log, 'log': tensorboard_logs}

    def multi_test_epoch_end(self, outputs, dataloader_idx: int = 0):
        if self.compute_eval_loss:
            test_loss_mean = torch.stack([x['test_loss'] for x in outputs]).mean()
            test_loss_log = {'test_loss': test_loss_mean}
        else:
            test_loss_log = {}
        wer_num = torch.stack([x['test_wer_num'] for x in outputs]).sum()
        wer_denom = torch.stack([x['test_wer_denom'] for x in outputs]).sum()
        tensorboard_logs = {**test_loss_log, 'test_wer': wer_num.float() / wer_denom}
        return {**test_loss_log, 'log': tensorboard_logs}


    def on_after_backward(self):
        super().on_after_backward()
        if self._optim_variational_noise_std > 0 and self.global_step >= self._optim_variational_noise_start:
            for param_name, param in self.decoder.named_parameters():
                if param.grad is not None:
                    noise = torch.normal(
                        mean=0.0,
                        std=self._optim_variational_noise_std,
                        size=param.size(),
                        device=param.device,
                        dtype=param.dtype,
                    )
                    param.grad.data.add_(noise)

        if self._optim_normalize_joint_txu:
            T, U = self._optim_normalize_txu
            if T is not None and U is not None:
                for param_name, param in self.encoder.named_parameters():
                    if param.grad is not None:
                        param.grad.data.div_(U)

                for param_name, param in self.decoder.named_parameters():
                    if param.grad is not None:
                        param.grad.data.div_(T)

        if self._optim_normalize_encoder_norm:
            for param_name, param in self.encoder.named_parameters():
                if param.grad is not None:
                    norm = param.grad.norm()
                    param.grad.data.div_(norm)

        if self._optim_normalize_decoder_norm:
            for param_name, param in self.decoder.named_parameters():
                if param.grad is not None:
                    norm = param.grad.norm()
                    param.grad.data.div_(norm)

        if self._optim_normalize_joint_norm:
            for param_name, param in self.joint.named_parameters():
                if param.grad is not None:
                    norm = param.grad.norm()
                    param.grad.data.div_(norm)

    def export(self, output: str, input_example=None, **kwargs):
        encoder_exp, encoder_descr = self.encoder.export(
            augment_filename(output, 'Encoder'), input_example=input_example, **kwargs,
        )
        decoder_joint = RNNTDecoderJoint(self.decoder, self.joint)
        decoder_exp, decoder_descr = decoder_joint.export(
            augment_filename(output, 'Decoder-Joint'),
            # TODO: propagate from export()
            input_example=None,
            **kwargs,
        )
        return encoder_exp + decoder_exp, encoder_descr + decoder_descr