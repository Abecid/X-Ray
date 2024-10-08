#/bin/bash

export MODEL_NAME="stabilityai/stable-video-diffusion-img2vid"
export OUTPUT_DIR="Output/ShapeNetV2_Car_1"
export INSTANCE_DIR="Data/ShapeNetV2_Car"
export NUM_GPUS=3

CUDA_VISIBLE_DEVICES=0,1,2 accelerate launch \
    --main_process_port=29500 --num_processes=${NUM_GPUS} train_diffusion.py \
    --pretrained_model_name_or_path=${MODEL_NAME} \
    --data_root=${INSTANCE_DIR} \
    --output_dir=${OUTPUT_DIR} \
    --per_gpu_batch_size=14 --gradient_accumulation_steps=1 \
    --max_train_steps=1000000 \
    --width=64 \
    --height=64 \
    --num_frames=8 \
    --checkpointing_steps=1000 --checkpoints_total_limit=3 \
    --learning_rate=4e-5 --lr_warmup_steps=0 \
    --seed=1234 \
    --num_workers=6 \
    --validation_steps=1000 \
    --num_validation_images=5 \
    --near 0.6 \
    --far 1.8 \
    --resume_from_checkpoint="latest"
    # --pretrain_unet='Output/ShapeNetV2_Car_1/checkpoint-796000'
