# Copyright (C) 2022. Huawei Technologies Co., Ltd. All rights reserved.
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

from nemo.collections.asr.models.configs.common_config import AudioDatasetConfig, AdamWParams, \
    CosineAnnealingParams, Conv2dBlock, Conv2dNormAct, Conv1dNormAct
from nemo.collections.asr.models.spec2vec.spec2vec_config import FeatureEncoderConfig, \
    ConvTransformerBlock, ProjectorConfig
from nemo.collections.asr.models.st2vec.st2vec_config import ST2VecEncoderConfig, ST2VecPretrainModelConfig, \
    ShiftPerturbConfig, NoisePerturbConfig
from nemo.collections.asr.models.wav2vec.wav2vec_config import Wav2VecTransformerEncoderConfig, \
    Wav2VecTransformerConfig, ConvConfig, Wav2VecActivationType, QuantizerConfig, Wav2VecMaskingConfig, Wav2VecMaskType, \
    LossConfig
from nemo.collections.asr.modules.audio_preprocessing import AudioToMelSpectrogramPreprocessorConfig
from nemo.core.config import TrainerConfig
from nemo.core.config.modelPT import ModelPTConfig
from nemo.utils.exp_manager import ExpManagerConfig, CallbackParams

config_name = 'st2vec'

sample_rate = 16000
num_features = 128

max_steps = 500000

st2vec_encoder = ST2VecEncoderConfig(
    preprocessor=AudioToMelSpectrogramPreprocessorConfig(
        normalize='per_feature',
        sample_rate=sample_rate,
        window_size=0.02,
        window_stride=0.01,
        window='hann',
        features=num_features,
        stft_conv=False,
        dither_train_only=True,
        normalize_time_domain=True,
    ),
    feature_encoder=FeatureEncoderConfig(
        feat_in=num_features,
        use_conv_mask=True,
        use_tf_pad=True,
        conv2d_block=None,
        conv_transformer_blocks=[ConvTransformerBlock(
            conv_layers=[Conv1dNormAct(filters=384, kernel_size=(5,), stride=(2,),
                                       norm_type='ln', bias=True, dropout=0.1,
                                       act_func='relu'),
                         Conv1dNormAct(filters=512, kernel_size=(5,), stride=(2,),
                                       norm_type='ln', bias=True, dropout=0.1,
                                       act_func='relu'),
                         Conv1dNormAct(filters=512, kernel_size=(1,), stride=(1,),
                                       norm_type='ln', bias=True,
                                       act_func=None),
                         ],
            transformer_block=Wav2VecTransformerConfig(
                use_pytorch_transformer=False,
                dropout=0.1,
                conv=ConvConfig(
                    conv_pos=128,
                    conv_pos_groups=16
                ),
                encoder=Wav2VecTransformerEncoderConfig(
                    encoder_layers=4,
                    encoder_layerdrop=0.05,
                    embedding_dim=512,
                    ffn_embedding_dim=512 * 4,
                    num_attention_heads=8,
                    dropout=0.1,
                    activation_fn=Wav2VecActivationType.gelu,
                    layer_norm_first=True
                )
            ),
        ),
            ConvTransformerBlock(
            conv_layers=[Conv1dNormAct(filters=1024 * 2, kernel_size=(5,), stride=(2,),
                                       norm_type='ln', bias=True, dropout=0.1,
                                       act_func='relu'),
                         Conv1dNormAct(filters=1024, kernel_size=(1,), stride=(1,),
                                       norm_type='ln', bias=True,
                                       act_func=None),
                         ],
            transformer_block=Wav2VecTransformerConfig(
                use_pytorch_transformer=False,
                dropout=0.1,
                conv=ConvConfig(
                    conv_pos=128,
                    conv_pos_groups=16
                ),
                encoder=Wav2VecTransformerEncoderConfig(
                    encoder_layers=20,
                    encoder_layerdrop=0.05,
                    embedding_dim=1024,
                    ffn_embedding_dim=4096,
                    num_attention_heads=16,
                    dropout=0.1,
                    activation_fn=Wav2VecActivationType.gelu,
                    layer_norm_first=True
                )
            ),
        ),
      ],
    ),
    masking=Wav2VecMaskingConfig(
        mask_prob=0.5,
        mask_type=Wav2VecMaskType.static,
        mask_emb_type='gaussian',
        mask_other=0,
        mask_length=20,
        no_mask_overlap=False,
        mask_min_space=1,
        mask_channel_prob=0.4,
        mask_channel_type=Wav2VecMaskType.static,
        mask_channel_other=0,
        mask_channel_length=20,
        no_mask_channel_overlap=False,
        mask_channel_min_space=1,
        mask_shrink_to_batch_min=False
    ),
    target_compute_perturb=True,
    target_shifting=ShiftPerturbConfig(
        dist='uniform',
        shift_prob=1.0,
        max_ratio=0.5,
        unit=8,
        max=16,
        min=0,
        truncate=False,
    ),
    target_momentum_type='cosine',
    target_momentum=0.99,
    target_momentum_final=0.999,
    target_momentum_steps=max_steps,
    projector=ProjectorConfig(output_dim=512),
    predictor=ProjectorConfig(
        conv_layers=[
            Conv1dNormAct(filters=512, kernel_size=(5,), stride=(1,),
                          norm_type='bn',
                          act_func='relu'),
            Conv1dNormAct(filters=512, kernel_size=(5,), stride=(1,),
                          norm_type='bn',
                          act_func='relu'),
        ],
        output_dim=512
    ),
    n_negatives=100,
    cross_sample_negatives=0,
    codebook_negatives=0,
    negatives_from_everywhere=False,
)

model = ST2VecPretrainModelConfig()

model.st2vec_encoder = st2vec_encoder

model.logit_temp = 0.3
model.loss = LossConfig(
    prob_ppl_weight=0.0
)


model.train_ds = AudioDatasetConfig(
    manifest_filepath='spanish_yt_train.json',
    sample_rate=sample_rate,
    batch_size=80,
    min_duration=2.0,
    crop_size=256000,
    shuffle=True,
    num_workers=14,
    pin_memory=True,
)

model.validation_ds = AudioDatasetConfig(
    manifest_filepath='spanish_yt_test.json',
    sample_rate=sample_rate,
    batch_size=80,
    min_duration=2.0,
    crop_size=256000,
    shuffle=False,
    num_workers=14,
)

model.test_ds = AudioDatasetConfig(
    manifest_filepath='spanish_yt_test.json',
    sample_rate=sample_rate,
    batch_size=80,
    min_duration=2.0,
    crop_size=256000,
    shuffle=False,
    num_workers=14,
)

model.expected_gpu_num = 8
model.optim = AdamWParams(
    lr=0.003,
    eps=1e-6,
    betas=[0.9, 0.98],
    weight_decay=0.05,
    sched=CosineAnnealingParams(
        min_lr=0.0,
        warmup_steps=32000,
        max_steps=max_steps,
    ),
)

noise_dir = 'DNS-noise'
model.noise_perturb = NoisePerturbConfig(
    manifest_path=["/media/NFS-mid/es_manifests/DNS-noise-train2.json"],
    min_snr_db=0.,
    max_snr_db=20.,
    ratio=0.3,
    target_sr=sample_rate,
    data_dir=noise_dir,
    cache_noise=True,
)
trainer = TrainerConfig(
    gpus=8,
    max_epochs=700,
    accelerator='ddp',
    accumulate_grad_batches=1,
    checkpoint_callback=False, # Provided by exp_manager
    logger=False,  # Provided by exp_manager
    log_every_n_steps=50,
    progress_bar_refresh_rate=50,
    num_sanity_val_steps=0,
    check_val_every_n_epoch=1,
    amp_level= 'O0'
)

exp_manager = ExpManagerConfig(
    name=config_name,
    create_checkpoint_callback=True,
    checkpoint_callback_params=CallbackParams(
        save_top_k=5
    )
)
cfg = ModelPTConfig(
    name=config_name,
    model=model,
    trainer=trainer,
    exp_manager=exp_manager
)