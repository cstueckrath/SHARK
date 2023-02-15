import sys
import torch
import time
from PIL import Image
from dataclasses import dataclass
from apps.stable_diffusion.src import (
    args,
    InpaintPipeline,
    get_schedulers,
    set_init_device_flags,
    utils,
    clear_all,
    save_output_img,
)


@dataclass
class Config:
    model_id: str
    ckpt_loc: str
    precision: str
    batch_size: int
    max_length: int
    height: int
    width: int
    device: str


inpaint_obj = None
config_obj = None
schedulers = None


# Exposed to UI.
def inpaint_inf(
    prompt: str,
    negative_prompt: str,
    image: Image,
    mask_image: Image,
    height: int,
    width: int,
    steps: int,
    guidance_scale: float,
    seed: int,
    batch_count: int,
    batch_size: int,
    scheduler: str,
    custom_model: str,
    hf_model_id: str,
    precision: str,
    device: str,
    max_length: int,
    save_metadata_to_json: bool,
    save_metadata_to_png: bool,
):
    global inpaint_obj
    global config_obj
    global schedulers

    args.prompts = [prompt]
    args.negative_prompts = [negative_prompt]
    args.guidance_scale = guidance_scale
    args.steps = steps
    args.scheduler = scheduler

    # set ckpt_loc and hf_model_id.
    types = (
        ".ckpt",
        ".safetensors",
    )  # the tuple of file types
    args.ckpt_loc = ""
    args.hf_model_id = ""
    if custom_model == "None":
        if not hf_model_id:
            return (
                None,
                "Please provide either custom model or huggingface model ID, both must not be empty",
            )
        args.hf_model_id = hf_model_id
    elif ".ckpt" in custom_model or ".safetensors" in custom_model:
        args.ckpt_loc = custom_model
    else:
        args.hf_model_id = custom_model

    args.save_metadata_to_json = save_metadata_to_json
    args.write_metadata_to_png = save_metadata_to_png

    dtype = torch.float32 if precision == "fp32" else torch.half
    cpu_scheduling = not scheduler.startswith("Shark")
    new_config_obj = Config(
        args.hf_model_id,
        args.ckpt_loc,
        precision,
        batch_size,
        max_length,
        height,
        width,
        device,
    )
    if config_obj != new_config_obj:
        config_obj = new_config_obj
        args.precision = precision
        args.batch_size = batch_size
        args.max_length = max_length
        args.height = height
        args.width = width
        args.device = device.split("=>", 1)[1].strip()
        args.iree_vulkan_target_triple = ""
        args.use_tuned = True
        args.import_mlir = False
        set_init_device_flags()
        model_id = (
            args.hf_model_id
            if args.hf_model_id
            else "stabilityai/stable-diffusion-2-inpainting"
        )
        schedulers = get_schedulers(model_id)
        scheduler_obj = schedulers[scheduler]
        inpaint_obj = InpaintPipeline.from_pretrained(
            scheduler_obj,
            args.import_mlir,
            args.hf_model_id,
            args.ckpt_loc,
            args.custom_vae,
            args.precision,
            args.max_length,
            args.batch_size,
            args.height,
            args.width,
            args.use_base_vae,
            args.use_tuned,
        )

    if not inpaint_obj:
        sys.exit("text to image pipeline must not return a null value")

    inpaint_obj.scheduler = schedulers[scheduler]

    start_time = time.time()
    inpaint_obj.log = ""
    generated_imgs = []
    seeds = []
    img_seed = utils.sanitize_seed(seed)
    for i in range(batch_count):
        if i > 0:
            img_seed = utils.sanitize_seed(-1)
        out_imgs = inpaint_obj.generate_images(
            prompt,
            negative_prompt,
            image,
            mask_image,
            batch_size,
            height,
            width,
            steps,
            guidance_scale,
            img_seed,
            args.max_length,
            dtype,
            args.use_base_vae,
            cpu_scheduling,
        )
        save_output_img(out_imgs[0], img_seed)
        generated_imgs.extend(out_imgs)
        seeds.append(img_seed)
        inpaint_obj.log += "\n"

    total_time = time.time() - start_time
    text_output = f"prompt={args.prompts}"
    text_output += f"\nnegative prompt={args.negative_prompts}"
    text_output += f"\nmodel_id={args.hf_model_id}, ckpt_loc={args.ckpt_loc}"
    text_output += f"\nscheduler={args.scheduler}, device={device}"
    text_output += f"\nsteps={args.steps}, guidance_scale={args.guidance_scale}, seed={seeds}"
    text_output += f"\nsize={args.height}x{args.width}, batch-count={batch_count}, batch-size={args.batch_size}, max_length={args.max_length}"
    text_output += inpaint_obj.log
    text_output += f"\nTotal image generation time: {total_time:.4f}sec"

    return generated_imgs, text_output


if __name__ == "__main__":
    if args.clear_all:
        clear_all()

    if args.img_path is None:
        print("Flag --img_path is required.")
        exit()
    if args.mask_path is None:
        print("Flag --mask_path is required.")
        exit()
    if "inpaint" not in args.hf_model_id:
        print("Please use inpainting model with --hf_model_id.")
        exit()

    dtype = torch.float32 if args.precision == "fp32" else torch.half
    cpu_scheduling = not args.scheduler.startswith("Shark")
    set_init_device_flags()
    schedulers = get_schedulers(args.hf_model_id)
    scheduler_obj = schedulers[args.scheduler]
    seed = args.seed
    image = Image.open(args.img_path)
    mask_image = Image.open(args.mask_path)

    inpaint_obj = InpaintPipeline.from_pretrained(
        scheduler_obj,
        args.import_mlir,
        args.hf_model_id,
        args.ckpt_loc,
        args.custom_vae,
        args.precision,
        args.max_length,
        args.batch_size,
        args.height,
        args.width,
        args.use_base_vae,
        args.use_tuned,
    )

    for current_batch in range(args.batch_count):
        if current_batch > 0:
            seed = -1
        seed = utils.sanitize_seed(seed)

        start_time = time.time()
        generated_imgs = inpaint_obj.generate_images(
            args.prompts,
            args.negative_prompts,
            image,
            mask_image,
            args.batch_size,
            args.height,
            args.width,
            args.steps,
            args.guidance_scale,
            seed,
            args.max_length,
            dtype,
            args.use_base_vae,
            cpu_scheduling,
        )
        total_time = time.time() - start_time
        text_output = f"prompt={args.prompts}"
        text_output += f"\nnegative prompt={args.negative_prompts}"
        text_output += (
            f"\nmodel_id={args.hf_model_id}, ckpt_loc={args.ckpt_loc}"
        )
        text_output += f"\nscheduler={args.scheduler}, device={args.device}"
        text_output += f"\nsteps={args.steps}, guidance_scale={args.guidance_scale}, seed={seed}, size={args.height}x{args.width}"
        text_output += (
            f", batch size={args.batch_size}, max_length={args.max_length}"
        )
        text_output += inpaint_obj.log
        text_output += f"\nTotal image generation time: {total_time:.4f}sec"

        save_output_img(generated_imgs[0], seed)
        print(text_output)
