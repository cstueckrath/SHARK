from diffusers import AutoencoderKL, UNet2DConditionModel
from transformers import CLIPTextModel
from collections import defaultdict
import torch
import traceback
import re
import sys
import os
from apps.stable_diffusion.src.utils import (
    compile_through_fx,
    get_opt_flags,
    base_models,
    args,
    fetch_or_delete_vmfbs,
    preprocessCKPT,
    get_path_to_diffusers_checkpoint,
    fetch_and_update_base_model_id,
    get_path_stem,
    get_extended_name,
)


# These shapes are parameter dependent.
def replace_shape_str(shape, max_len, width, height, batch_size):
    new_shape = []
    for i in range(len(shape)):
        if shape[i] == "max_len":
            new_shape.append(max_len)
        elif shape[i] == "height":
            new_shape.append(height)
        elif shape[i] == "width":
            new_shape.append(width)
        elif isinstance(shape[i], str):
            mul_val = int(shape[i].split("*")[0])
            if "batch_size" in shape[i]:
                new_shape.append(batch_size * mul_val)
            elif "height" in shape[i]:
                new_shape.append(height * mul_val)
            elif "width" in shape[i]:
                new_shape.append(width * mul_val)
        else:
            new_shape.append(shape[i])
    return new_shape


# Get the input info for various models i.e. "unet", "clip", "vae", "vae_encode".
def get_input_info(model_info, max_len, width, height, batch_size):
    dtype_config = {"f32": torch.float32, "i64": torch.int64}
    input_map = defaultdict(list)
    for k in model_info:
        for inp in model_info[k]:
            shape = model_info[k][inp]["shape"]
            dtype = dtype_config[model_info[k][inp]["dtype"]]
            tensor = None
            if isinstance(shape, list):
                clean_shape = replace_shape_str(
                    shape, max_len, width, height, batch_size
                )
                if dtype == torch.int64:
                    tensor = torch.randint(1, 3, tuple(clean_shape))
                else:
                    tensor = torch.randn(*clean_shape).to(dtype)
            elif isinstance(shape, int):
                tensor = torch.tensor(shape).to(dtype)
            else:
                sys.exit("shape isn't specified correctly.")
            input_map[k].append(tensor)
    return input_map


class SharkifyStableDiffusionModel:
    def __init__(
        self,
        model_id: str,
        custom_weights: str,
        custom_vae: str,
        precision: str,
        max_len: int = 64,
        width: int = 512,
        height: int = 512,
        batch_size: int = 1,
        use_base_vae: bool = False,
        use_tuned: bool = False,
        debug: bool = False,
        sharktank_dir: str = "",
        generate_vmfb: bool = True,
    ):
        self.check_params(max_len, width, height)
        self.max_len = max_len
        self.height = height // 8
        self.width = width // 8
        self.batch_size = batch_size
        self.custom_weights = custom_weights
        if custom_weights != "":
            assert custom_weights.lower().endswith(
                (".ckpt", ".safetensors")
            ), "checkpoint files supported can be any of [.ckpt, .safetensors] type"
            custom_weights = get_path_to_diffusers_checkpoint(custom_weights)
        self.model_id = model_id if custom_weights == "" else custom_weights
        self.custom_vae = custom_vae
        self.precision = precision
        self.base_vae = use_base_vae
        self.model_name = (
            "_"
            + str(batch_size)
            + "_"
            + str(max_len)
            + "_"
            + str(height)
            + "_"
            + str(width)
            + "_"
            + precision
        )
        print(f'use_tuned? sharkify: {use_tuned}')
        self.use_tuned = use_tuned
        if use_tuned:
            self.model_name = self.model_name + "_tuned"
        self.model_name = self.model_name + "_" + get_path_stem(self.model_id)

        print(self.model_name)
        self.debug = debug
        self.sharktank_dir = sharktank_dir
        self.generate_vmfb = generate_vmfb

    def get_extended_name_for_all_model(self):
        model_name = {}
        sub_model_list = ["clip", "unet", "vae", "vae_encode"]
        for model in sub_model_list:
            sub_model = model
            model_config = self.model_name
            if "vae" == model:
                if self.custom_vae != "":
                    model_config = model_config + get_path_stem(self.custom_vae)
                if self.base_vae:
                    sub_model = "base_vae"
            model_name[model] = get_extended_name(sub_model + model_config)
        return model_name

    def check_params(self, max_len, width, height):
        if not (max_len >= 32 and max_len <= 77):
            sys.exit("please specify max_len in the range [32, 77].")
        if not (width % 8 == 0 and width >= 384):
            sys.exit("width should be greater than 384 and multiple of 8")
        if not (height % 8 == 0 and height >= 384):
            sys.exit("height should be greater than 384 and multiple of 8")

    def get_vae_encode(self):
        class VaeEncodeModel(torch.nn.Module):
            def __init__(self, model_id=self.model_id):
                super().__init__()
                self.vae = AutoencoderKL.from_pretrained(
                    model_id,
                    subfolder="vae",
                )

            def forward(self, input):
                latents = self.vae.encode(input).latent_dist.sample()
                return 0.18215 * latents

        vae_encode = VaeEncodeModel()
        inputs = tuple(self.inputs["vae_encode"])
        is_f16 = True if self.precision == "fp16" else False
        shark_vae_encode = compile_through_fx(
            vae_encode,
            inputs,
            is_f16=is_f16,
            use_tuned=self.use_tuned,
            model_name=self.model_name["vae_encode"],
            extra_args=get_opt_flags("vae", precision=self.precision),
        )
        return shark_vae_encode

    def get_vae(self):
        class VaeModel(torch.nn.Module):
            def __init__(self, model_id=self.model_id, base_vae=self.base_vae, custom_vae=self.custom_vae):
                super().__init__()
                self.vae = AutoencoderKL.from_pretrained(
                    model_id if custom_vae == "" else custom_vae,
                    subfolder="vae",
                )
                self.base_vae = base_vae

            def forward(self, input):
                if not self.base_vae:
                    input = 1 / 0.18215 * input
                x = self.vae.decode(input, return_dict=False)[0]
                x = (x / 2 + 0.5).clamp(0, 1)
                if self.base_vae:
                    return x
                x = x * 255.0
                return x.round()

        vae = VaeModel()
        inputs = tuple(self.inputs["vae"])
        is_f16 = True if self.precision == "fp16" else False
        save_dir = os.path.join(self.sharktank_dir, self.model_name["vae"])
        if self.debug:
            os.makedirs(save_dir, exist_ok=True)
        shark_vae = compile_through_fx(
            vae,
            inputs,
            is_f16=is_f16,
            use_tuned=self.use_tuned,
            model_name=self.model_name["vae"],
            debug=self.debug,
            generate_vmfb=self.generate_vmfb,
            save_dir=save_dir,
            extra_args=get_opt_flags("vae", precision=self.precision),
        )
        return shark_vae

    def get_unet(self):
        class UnetModel(torch.nn.Module):
            def __init__(self, model_id=self.model_id):
                super().__init__()
                self.unet = UNet2DConditionModel.from_pretrained(
                    model_id,
                    subfolder="unet",
                )
                self.in_channels = self.unet.in_channels
                self.train(False)

            def forward(
                self, latent, timestep, text_embedding, guidance_scale
            ):
                # expand the latents if we are doing classifier-free guidance to avoid doing two forward passes.
                latents = torch.cat([latent] * 2)
                unet_out = self.unet.forward(
                    latents, timestep, text_embedding, return_dict=False
                )[0]
                noise_pred_uncond, noise_pred_text = unet_out.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (
                    noise_pred_text - noise_pred_uncond
                )
                return noise_pred

        unet = UnetModel()
        is_f16 = True if self.precision == "fp16" else False
        inputs = tuple(self.inputs["unet"])
        input_mask = [True, True, True, False]
        save_dir = os.path.join(self.sharktank_dir, self.model_name["unet"])
        if self.debug:
            os.makedirs(
                save_dir,
                exist_ok=True,
            )
        shark_unet = compile_through_fx(
            unet,
            inputs,
            model_name=self.model_name["unet"],
            is_f16=is_f16,
            f16_input_mask=input_mask,
            use_tuned=self.use_tuned,
            debug=self.debug,
            generate_vmfb=self.generate_vmfb,
            save_dir=save_dir,
            extra_args=get_opt_flags("unet", precision=self.precision),
        )
        return shark_unet

    def get_clip(self):
        class CLIPText(torch.nn.Module):
            def __init__(self, model_id=self.model_id):
                super().__init__()
                self.text_encoder = CLIPTextModel.from_pretrained(
                    model_id,
                    subfolder="text_encoder",
                )

            def forward(self, input):
                return self.text_encoder(input)[0]

        clip_model = CLIPText()
        save_dir = os.path.join(self.sharktank_dir, self.model_name["clip"])
        if self.debug:
            os.makedirs(
                save_dir,
                exist_ok=True,
            )
        shark_clip = compile_through_fx(
            clip_model,
            tuple(self.inputs["clip"]),
            model_name=self.model_name["clip"],
            debug=self.debug,
            generate_vmfb=self.generate_vmfb,
            save_dir=save_dir,
            extra_args=get_opt_flags("clip", precision="fp32"),
        )
        return shark_clip

    # Compiles Clip, Unet and Vae with `base_model_id` as defining their input
    # configiration.
    def compile_all(self, base_model_id, need_vae_encode):
        self.inputs = get_input_info(
            base_models[base_model_id],
            self.max_len,
            self.width,
            self.height,
            self.batch_size,
        )
        compiled_unet = self.get_unet()
        if self.custom_vae != "":
            print("Plugging in custom Vae")
        compiled_vae = self.get_vae()
        compiled_clip = self.get_clip()
        if need_vae_encode:
            compiled_vae_encode = self.get_vae_encode()
            return compiled_clip, compiled_unet, compiled_vae, compiled_vae_encode

        return compiled_clip, compiled_unet, compiled_vae

    def __call__(self):
        # Step 1:
        # --  Fetch all vmfbs for the model, if present, else delete the lot.
        need_vae_encode = args.img_path is not None
        self.model_name = self.get_extended_name_for_all_model()
        vmfbs = fetch_or_delete_vmfbs(self.model_name, need_vae_encode, self.precision)   
        if vmfbs[0]:
            # -- If all vmfbs are indeed present, we also try and fetch the base
            #    model configuration for running SD with custom checkpoints.
            if self.custom_weights != "":
                args.hf_model_id = fetch_and_update_base_model_id(self.custom_weights)
            if args.hf_model_id == "":
                sys.exit("Base model configuration for the custom model is missing. Use `--clear_all` and re-run.")
            print("Loaded vmfbs from cache and successfully fetched base model configuration.")
            return vmfbs

        # Step 2:
        # -- If vmfbs weren't found, we try to see if the base model configuration
        #    for the required SD run is known to us and bypass the retry mechanism.
        model_to_run = ""
        if self.custom_weights != "":
            model_to_run = self.custom_weights
            assert self.custom_weights.lower().endswith(
                (".ckpt", ".safetensors")
            ), "checkpoint files supported can be any of [.ckpt, .safetensors] type"
            preprocessCKPT(self.custom_weights)
        else:
            model_to_run = args.hf_model_id
        # For custom Vae user can provide either the repo-id or a checkpoint file,
        # and for a checkpoint file we'd need to process it via Diffusers' script.
        if self.custom_vae.lower().endswith((".ckpt", ".safetensors")):
            preprocessCKPT(self.custom_vae)
            self.custom_vae = get_path_to_diffusers_checkpoint(self.custom_vae)
        base_model_fetched = fetch_and_update_base_model_id(model_to_run)
        if base_model_fetched != "":
            print("Compiling all the models with the fetched base model configuration.")
            if args.ckpt_loc != "":
                args.hf_model_id = base_model_fetched
            return self.compile_all(base_model_fetched, need_vae_encode)

        # Step 3:
        # -- This is the retry mechanism where the base model's configuration is not
        #    known to us and figure that out by trial and error.
        print("Inferring base model configuration.")
        for model_id in base_models:
            try:
                if need_vae_encode:
                    compiled_clip, compiled_unet, compiled_vae, compiled_vae_encode = self.compile_all(model_id, need_vae_encode)
                else:
                    compiled_clip, compiled_unet, compiled_vae = self.compile_all(model_id, need_vae_encode)
            except Exception as e:
                print("Retrying with a different base model configuration")
                continue
            # -- Once a successful compilation has taken place we'd want to store
            #    the base model's configuration inferred.
            fetch_and_update_base_model_id(model_to_run, model_id)
            # This is done just because in main.py we are basing the choice of tokenizer and scheduler
            # on `args.hf_model_id`. Since now, we don't maintain 1:1 mapping of variants and the base
            # model and rely on retrying method to find the input configuration, we should also update
            # the knowledge of base model id accordingly into `args.hf_model_id`.
            if args.ckpt_loc != "":
                args.hf_model_id = model_id
            if need_vae_encode:
                return (
                    compiled_clip,
                    compiled_unet,
                    compiled_vae,
                    compiled_vae_encode,
                )
            return compiled_clip, compiled_unet, compiled_vae
        sys.exit(
            "Cannot compile the model. Please create an issue with the detailed log at https://github.com/nod-ai/SHARK/issues"
        )
