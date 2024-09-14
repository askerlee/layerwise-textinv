import torch
import torch.nn as nn
from transformers import CLIPTokenizer, CLIPImageProcessor
from .arc2face_models import CLIPTextModelWrapper
from .subj_basis_generator import SubjBasisGenerator
from ConsistentID.lib.pipline_ConsistentID import ConsistentIDPipeline 
from .util import perturb_tensor, pad_image_obj_to_square, \
                  calc_stats, patch_clip_image_encoder_with_mask, CLIPVisionModelWithMask
import torch.nn.functional as F
import numpy as np
import cv2
from PIL import Image
from insightface.app import FaceAnalysis
import os

class FaceID2AdaPrompt(nn.Module):
    # To be initialized in derived classes.
    def __init__(self, *args, **kwargs):
        super().__init__()
        # Initialize model components.
        # These components of ConsistentID_ID2AdaPrompt will be shared with the teacher model.
        # So we don't initialize them in the ctor(), but borrow them from the teacher model.
        # These components of Arc2Face_ID2AdaPrompt will be initialized in its ctor().
        self.clip_image_encoder             = None
        self.clip_preprocessor              = None
        self.face_app                       = None
        self.text_to_image_prompt_encoder   = None
        self.tokenizer                      = None
        self.dtype                          = kwargs.get('dtype', torch.float16)

        # Load Img2Ada SubjectBasisGenerator.
        self.subject_string                 = kwargs.get('subject_string', 'z')
        self.adaface_ckpt_path              = kwargs.get('adaface_ckpt_path', None)
        self.subj_basis_generator           = None
        # -1: use the default scale for the adaface encoder type.
        # i.e., 6 for arc2face and 1 for consistentID.
        self.out_id_embs_cfg_scale          = kwargs.get('out_id_embs_cfg_scale', -1)
        self.to_load_id2img_learnable_modules  = kwargs.get('to_load_id2img_learnable_modules', True)

        # Set model behavior configurations.
        self.gen_neg_img_prompt             = False
        self.use_clip_embs                  = False
        self.do_contrast_clip_embs          = False
        # num_id_vecs as the output embeddings of the ID2ImgPrompt module, 
        # as well as the output embeddings of the subject basis generator.
        self.num_id_vecs                    = -1
        self.id_img_prompt_max_length       = 77
        self.clip_embedding_dim             = 1024
        self.name                           = None

    # image_objs: a list of np array / tensor / Image objects of different sizes [Hi, Wi].
    # If image_objs is a list of tensors, then each tensor should be [3, Hi, Wi].
    # If image_objs is None, then image_paths should be provided, 
    # and image_objs will be loaded from image_paths.
    # fg_masks: None, or a list of [Hi, Wi].
    def extract_init_id_embeds_from_images(self, image_objs, image_paths, fg_masks=None, 
                                           size=(512, 512), calc_avg=False, 
                                           skip_non_faces=True, 
                                           return_clip_embs=None, do_contrast_clip_embs=None, 
                                           verbose=False):
        # If return_clip_embs and do_contrast_clip_embs are not provided, then use the default values.
        if return_clip_embs is None:
            return_clip_embs = self.use_clip_embs
        if do_contrast_clip_embs is None:
            do_contrast_clip_embs = self.do_contrast_clip_embs

        # clip_image_encoder should be already put on GPU. 
        # So its .device is the device of its parameters.
        device = self.clip_image_encoder.device

        image_pixel_values  = []
        all_id_embs         = []
        faceless_img_count  = 0

        if image_objs is None and image_paths is not None:
            image_objs = []
            for image_path in image_paths:
                image_obj = Image.open(image_path)
                image_objs.append(image_obj)
            print(f'Loaded {len(image_objs)} images from {image_paths[0]}...')

        # image_objs could be a batch of images that have been collated into a tensor or np array.
        # image_objs can also be a list of images.
        # The code below that processes them one by one can be applied in both cases.
        # If image_objs are a collated batch, processing them one by one will not add much overhead.
        for idx, image_obj in enumerate(image_objs):
            if return_clip_embs:
                # input to clip_preprocessor: an image or a batch of images, each being PIL.Image.Image, numpy.ndarray, 
                # torch.Tensor, tf.Tensor or jax.ndarray.
                # Different sizes of images are standardized to the same size 224*224.
                clip_image_pixel_values = self.clip_preprocessor(images=image_obj, return_tensors="pt").pixel_values
                image_pixel_values.append(clip_image_pixel_values)

            # Convert tensor to numpy array.
            if isinstance(image_obj, torch.Tensor):
                image_obj = image_obj.cpu().numpy().transpose(1, 2, 0)
            if isinstance(image_obj, np.ndarray):
                image_obj = Image.fromarray(image_obj)
            # Resize image_obj to (512, 512). The scheme is Image.NEAREST, to be consistent with 
            # PersonalizedBase dataset class.
            image_obj, _, _ = pad_image_obj_to_square(image_obj)
            image_np = np.array(image_obj.resize(size, Image.NEAREST))
            face_info = self.face_app.get(cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR))
            if len(face_info) == 0 and not skip_non_faces:
                print(f'No face detected in {image_paths[idx]}. Replace with random face embedding.')
                # If no face is detected (e.g. animals or bad images), then use a random tensor as the face embedding.
                id_emb = torch.randn(512)
                faceless_img_count += 1
            elif len(face_info) > 0:
                face_info = sorted(face_info, key=lambda x:(x['bbox'][2]-x['bbox'][0])*x['bbox'][3]-x['bbox'][1])[-1] # only use the maximum face
                # id_emb: [512,]
                id_emb = torch.from_numpy(face_info.normed_embedding)
            else:
                # len(face_info) == 0 and skip_non_faces.
                # Skip images without faces.
                print(f'Skip image with no face: {image_paths[idx]}')
                continue

            all_id_embs.append(id_emb)

        if verbose:
            print(f'{len(all_id_embs)} face images identified, {faceless_img_count} faceless images.')
        # all_id_embs: [BS, 512].
        all_id_embs = torch.stack(all_id_embs, dim=0).to(device=device, dtype=torch.float16)

        if return_clip_embs:
            # image_pixel_values: [BS, 3, 224, 224]
            image_pixel_values = torch.cat(image_pixel_values, dim=0)
            image_pixel_values = image_pixel_values.to(device=device, dtype=torch.float16)

            if fg_masks is not None:
                assert len(fg_masks) == len(image_objs)
                # fg_masks is a list of masks.
                if isinstance(fg_masks, (list, tuple)):
                    fg_masks2 = []
                    for fg_mask in fg_masks:
                        # fg_mask: [Hi, Wi]
                        # BUG: clip_preprocessor will do central crop on images. But fg_mask is not central cropped.
                        # If the ref image is not square, then the fg_mask will not match the image.
                        # TODO: crop fg_mask and images to square before calling extract_init_id_embeds_from_images().
                        # fg_mask2: [Hi, Wi] -> [1, 1, 224, 224]            
                        fg_mask2 = torch.tensor(fg_mask, device=device, dtype=torch.float16).unsqueeze(0).unsqueeze(0)
                        fg_mask2 = F.interpolate(fg_mask2, size=image_pixel_values.shape[-2:], mode='bilinear', align_corners=False)
                        fg_masks2.append(fg_mask2)
                    # fg_masks2: [BS, 224, 224]
                    fg_masks2 = torch.cat(fg_masks2, dim=0).squeeze(1)
                else:
                    # fg_masks is a collated batch of masks.
                    # The actual size doesn't matter, 
                    # as fg_mask2 will be resized to the same size as image features 
                    # (much smaller than image_pixel_values).            
                    fg_masks2 = fg_masks.to(device=device, dtype=torch.float16).unsqueeze(1)
                    # F.interpolate() always return a copy, even if scale_factor=1. So we don't need to clone fg_masks2.
                    fg_masks2 = F.interpolate(fg_masks2, size=image_pixel_values.shape[-2:], mode='bilinear', align_corners=False)
                    fg_masks2 = fg_masks2.squeeze(1)
            else:
                # fg_mask2: [BS, 224, 224]. 
                fg_masks2 = torch.ones_like(image_pixel_values[:, 0, :, :], device=device, dtype=torch.float16)

            with torch.no_grad():
                # neg_pixel_values: [1, 3, 224, 224]. clip_neg_features is invariant to the actual image.
                neg_pixel_values = torch.zeros_like(image_pixel_values[:1])
                clip_neg_features = self.clip_image_encoder(neg_pixel_values, attn_mask=None, output_hidden_states=True).hidden_states[-2]
                clip_neg_features = clip_neg_features.repeat(image_pixel_values.shape[0], 1, 1)

                # image_fg_features: [BS, 257, 1280]. 257: 16*16 (patch_embeds) + 1 (class_embeds).
                image_fg_dict  = self.clip_image_encoder(image_pixel_values, attn_mask=fg_masks2, output_hidden_states=True)
                # attn_mask: [BS, 1, 257]
                image_fg_features = image_fg_dict.hidden_states[-2]
                if do_contrast_clip_embs:
                    image_fg_features = image_fg_features - clip_neg_features
                if image_fg_dict.attn_mask is not None:
                    image_fg_features = image_fg_features * image_fg_dict.attn_mask

                # A negative mask is used to extract the background features.
                # If fg_masks is None, then fg_masks2 is all ones, and bg masks is all zeros.
                # Therefore, all pixels are masked. The extracted image_bg_features will be 
                # meaningless in this case.
                image_bg_dict  = self.clip_image_encoder(image_pixel_values, attn_mask=1-fg_masks2, output_hidden_states=True)
                image_bg_features = image_bg_dict.hidden_states[-2]
                if do_contrast_clip_embs:
                    image_bg_features = image_bg_features - clip_neg_features                
                if image_bg_dict.attn_mask is not None:
                    image_bg_features = image_bg_features * image_bg_dict.attn_mask        

            # clip_fgbg_features: [BS, 514, 1280]. 514 = 257*2.
            # all_id_embs:   [BS, 512].
            clip_fgbg_features = torch.cat([image_fg_features, image_bg_features], dim=1)
        else:
            clip_fgbg_features = None
            clip_neg_features  = None

        if calc_avg:
            if return_clip_embs:
                # clip_fgbg_features: [BS, 514, 1280] -> [1, 514, 1280].
                # all_id_embs:       [BS, 512]       -> [1, 512].
                clip_fgbg_features = clip_fgbg_features.mean(dim=0, keepdim=True)
                clip_neg_features  = clip_neg_features.mean(dim=0, keepdim=True)

            debug = False
            if debug and all_id_embs is not None:
                print(image_paths)                    
                calc_stats('all_id_embs', all_id_embs)
                # Compute pairwise similarities of the embeddings.
                all_id_embs = F.normalize(all_id_embs, p=2, dim=1)
                pairwise_sim = torch.matmul(all_id_embs, all_id_embs.t())
                print('pairwise_sim:', pairwise_sim)
                top_dir = os.path.dirname(image_paths[0]) 
                mean_emb_path = os.path.join(top_dir, "mean_emb.pt")
                if os.path.exists(mean_emb_path):
                    mean_emb = torch.load(mean_emb_path)
                    sim_to_mean = torch.matmul(all_id_embs, mean_emb.t())
                    print('sim_to_mean:', sim_to_mean)

            if all_id_embs is not None:
                id_embs = all_id_embs.mean(dim=0, keepdim=True)
                # Without normalization, id_embs.norm(dim=1) is ~0.9. So normalization doesn't have much effect.
                id_embs = F.normalize(id_embs, p=2, dim=-1)
            # id_embs is None only if insightface_app is None, i.e., disabled by the user.
        else:
            # Don't do average of all_id_embs.
            id_embs = all_id_embs
                    
        return faceless_img_count, id_embs, clip_fgbg_features, clip_neg_features

    # This function should be implemented in derived classes.
    # We don't plan to fine-tune the ID2ImgPrompt module. So disable the gradient computation.
    def map_init_id_to_img_prompt_embs(self, init_id_embs, 
                                       clip_features=None,
                                       called_for_neg_img_prompt=False,
                                       return_full_and_core_embs=True):
        raise NotImplementedError
        
    # If init_id_embs/pre_clip_features is provided, then use the provided face embeddings.
    # Otherwise, if image_paths/image_objs are provided, extract face embeddings from the images.
    # Otherwise, we generate random face embeddings [id_batch_size, 512].
    def get_img_prompt_embs(self, init_id_embs, pre_clip_features, image_paths, image_objs,
                            id_batch_size, perturb_std=0.0, 
                            skip_non_faces=True, return_core_id_embs_only=True,
                            avg_at_stage=None,  # id_emb, prompt_emb, or None.
                            id2img_prompt_encoder_trainable=False, verbose=False):
        face_image_count = 0
        device = self.clip_image_encoder.device

        if init_id_embs is None:
            # Input images are not provided. Generate random face embeddings.
            if image_paths is None and image_objs is None:
                faceid_embeds_from_images = False
                # Use random face embeddings as faceid_embeds. [BS, 512].
                faceid_embeds       = torch.randn(id_batch_size, 512).to(device=device, dtype=torch.float16)
                # Since it's a batch of random IDs, the CLIP features are all zeros as a placeholder.
                # Only ConsistentID_ID2AdaPrompt will use clip_fgbg_features and clip_neg_features.
                # Experiments show that using random clip features yields much better images than using zeros.
                clip_fgbg_features  = torch.randn(id_batch_size, 514, 1280).to(device=device, dtype=torch.float16) \
                                        if self.use_clip_embs else None
                clip_neg_features   = torch.randn(id_batch_size, 257, 1280).to(device=device, dtype=torch.float16) \
                                        if self.use_clip_embs else None
            else:
                # Extract face ID embeddings and CLIP features from the images.
                faceid_embeds_from_images = True
                faceless_img_count, faceid_embeds, clip_fgbg_features, clip_neg_features \
                    = self.extract_init_id_embeds_from_images( \
                        image_objs, image_paths=image_paths, size=(512, 512), 
                        calc_avg=(avg_at_stage == 'id_emb'), 
                        skip_non_faces=skip_non_faces, 
                        verbose=verbose)

                if image_paths is not None:
                    face_image_count = len(image_paths) - faceless_img_count
                else:
                    face_image_count = len(image_objs)  - faceless_img_count
        else:
            faceid_embeds_from_images = False
            # Use the provided init_id_embs as faceid_embeds.
            faceid_embeds = init_id_embs
            if pre_clip_features is not None:
                clip_fgbg_features, clip_neg_features = pre_clip_features
            else:
                clip_fgbg_features, clip_neg_features = None, None

            if init_id_embs.shape[0] == 1:
                faceid_embeds = faceid_embeds.repeat(id_batch_size, 1)
                if clip_fgbg_features is not None:
                    clip_fgbg_features = clip_fgbg_features.repeat(id_batch_size, 1, 1)
                if clip_neg_features is not None:
                    clip_neg_features  = clip_neg_features.repeat(id_batch_size, 1, 1)

        if perturb_std > 0:
            # If id_batch_size > 1, after adding noises, the id_batch_size embeddings will be different.
            faceid_embeds = perturb_tensor(faceid_embeds, perturb_std, perturb_std_is_relative=True, keep_norm=True)
            if self.name == 'consistentID':
                clip_fgbg_features = perturb_tensor(clip_fgbg_features, perturb_std, perturb_std_is_relative=True, keep_norm=True)
                # Don't perturb clip_neg_features, as it's a constant tensor.
                # clip_neg_features  = perturb_tensor(clip_neg_features,  perturb_std, perturb_std_is_relative=True, keep_norm=True)
                #faceid_embeds.normal_()

        faceid_embeds = F.normalize(faceid_embeds, p=2, dim=-1)

        # pos_prompt_embs, neg_prompt_embs: [BS, 77, 768] or [BS, 22, 768].
        with torch.set_grad_enabled(id2img_prompt_encoder_trainable):
            pos_prompt_embs, pos_core_prompt_emb  = \
                self.map_init_id_to_img_prompt_embs(faceid_embeds, clip_fgbg_features,
                                                    called_for_neg_img_prompt=False,
                                                    return_full_and_core_embs=True)
        
        if avg_at_stage == 'prompt_emb':
            pos_prompt_embs     = pos_prompt_embs.mean(dim=0, keepdim=True)
            pos_core_prompt_emb = pos_core_prompt_emb.mean(dim=0, keepdim=True)

        if return_core_id_embs_only:
            pos_prompt_embs = pos_core_prompt_emb

        # If faceid_embeds_from_images, and the prompt embeddings are already averaged, then 
        # we assume all images are from the same subject, and the batch dim of faceid_embeds is 1. 
        # So we need to repeat faceid_embeds.
        if faceid_embeds_from_images and avg_at_stage is not None:
            faceid_embeds   = faceid_embeds.repeat(id_batch_size, 1)
            pos_prompt_embs = pos_prompt_embs.repeat(id_batch_size, 1, 1)
            if clip_fgbg_features is not None:
                clip_fgbg_features = clip_fgbg_features.repeat(id_batch_size, 1, 1)
            if clip_neg_features is not None:
                clip_neg_features  = clip_neg_features.repeat(id_batch_size, 1, 1)
            
        if self.gen_neg_img_prompt:
            with torch.set_grad_enabled(id2img_prompt_encoder_trainable):
                neg_prompt_embs, neg_core_prompt_emb = \
                    self.map_init_id_to_img_prompt_embs(torch.zeros_like(faceid_embeds),
                                                        clip_neg_features,
                                                        called_for_neg_img_prompt=True,
                                                        return_full_and_core_embs=True)
                if return_core_id_embs_only:
                    neg_prompt_embs = neg_core_prompt_emb

            return face_image_count, faceid_embeds, pos_prompt_embs, neg_prompt_embs
        else:
            return face_image_count, faceid_embeds, pos_prompt_embs, None

    # NOTE: get_batched_img_prompt_embs() should only be called during training.
    # It is a wrapper of get_img_prompt_embs() which is convenient for batched training.
    # If init_id_embs is None, generate random face embeddings [BS, 512].
    # Returns faceid_embeds, id2img_prompt_emb.
    def get_batched_img_prompt_embs(self, batch_size, init_id_embs, pre_clip_features, 
                                    id2img_prompt_encoder_trainable=False):
        # pos_prompt_embs, neg_prompt_embs are generated without gradient computation.
        # So we don't need to worry that the teacher model weights are updated.
        return self.get_img_prompt_embs(init_id_embs=init_id_embs,
                                        pre_clip_features=pre_clip_features,
                                        image_paths=None,
                                        image_objs=None, 
                                        id_batch_size=batch_size,
                                        # During training, don't skip non-face images. Instead, 
                                        # setting skip_non_faces=False will replace them by random face embeddings.
                                        skip_non_faces=False,
                                        return_core_id_embs_only=True, 
                                        avg_at_stage=None, 
                                        id2img_prompt_encoder_trainable=id2img_prompt_encoder_trainable,
                                        verbose=False)

    def get_id2img_learnable_modules(self):
        raise NotImplementedError
    
    def load_id2img_learnable_modules(self, id2img_learnable_modules_state_dict_list):
        id2img_prompt_encoder_learnable_modules = self.get_id2img_learnable_modules()
        for module, state_dict in zip(id2img_prompt_encoder_learnable_modules, id2img_learnable_modules_state_dict_list):
            module.load_state_dict(state_dict)
        print(f'{len(id2img_prompt_encoder_learnable_modules)} ID2ImgPrompt encoder modules loaded.')
    
    def load_adaface_ckpt(self, adaface_ckpt_path):
        ckpt = torch.load(adaface_ckpt_path, map_location='cpu')
        string_to_subj_basis_generator_dict = ckpt["string_to_subj_basis_generator_dict"]
        if self.subject_string not in string_to_subj_basis_generator_dict:
            print(f"Subject '{self.subject_string}' not found in the embedding manager.")
            breakpoint()

        self.subj_basis_generator = string_to_subj_basis_generator_dict[self.subject_string]
        # In the original ckpt, num_out_layers is 16 for layerwise embeddings. 
        # But we don't do layerwise embeddings here, so we set it to 1.
        self.subj_basis_generator.num_out_layers = 1
        self.subj_basis_generator.patch_old_subj_basis_generator_ckpt()
        print(f"{adaface_ckpt_path}: loaded subject basis generator for '{self.subject_string}'.")
        print(repr(self.subj_basis_generator))

        if 'id2img_prompt_encoder_learnable_modules' in ckpt:
            if self.to_load_id2img_learnable_modules:
                self.load_id2img_learnable_modules(ckpt['id2img_prompt_encoder_learnable_modules'])
            else:
                print(f'ID2ImgPrompt encoder learnable modules in {adaface_ckpt_path} are not loaded.')

    # image_paths: a list of image paths. image_folder: the parent folder name.
    def generate_adaface_embeddings(self, image_paths, face_id_embs=None, gen_rand_face=False, 
                                    perturb_std=0, id2img_prompt_encoder_trainable=False):
        # faceid_embeds is a batch of extracted face analysis embeddings (BS * 512 = id_batch_size * 512).
        # If gen_rand_face, faceid_embeds/id_prompt_embs is a batch of random embeddings, each instance is different.
        # Otherwise, face_id_embs is used.
        # faceid_embeds is in the face analysis embeddings. id_prompt_embs is in the image prompt space.
        # Here id_batch_size = 1, so
        # faceid_embeds: [1, 512]. NOT used later.
        # id_prompt_embs: [1, 16/4, 768]. 
        # NOTE: Since return_core_id_embs_only is True, id_prompt_embs is only the 16 core ID embeddings.
        # arc2face prompt template: "photo of a id person"
        # ID embeddings start from "id person ...". So there are 3 template tokens before the 16 ID embeddings.
        face_image_count, faceid_embeds, id_prompt_embs, teacher_neg_id_prompt_embs \
            = self.get_img_prompt_embs(\
                init_id_embs=None if gen_rand_face else face_id_embs,
                pre_clip_features=None,
                # image_folder is passed only for logging purpose. 
                # image_paths contains the paths of the images.
                image_paths=image_paths, image_objs=None,
                id_batch_size=1, perturb_std=perturb_std, 
                return_core_id_embs_only=True, avg_at_stage='id_emb',
                id2img_prompt_encoder_trainable=id2img_prompt_encoder_trainable,
                verbose=True)
        
        if face_image_count == 0:
            return None, None
        
        # adaface_subj_embs: [16/4, 768]. 
        # adaface_prompt_embs: [1, 77, 768] (not used).
        # The adaface_prompt_embs_inf_type doesn't matter, since it only affects 
        # adaface_prompt_embs, which is not used.
        adaface_subj_embs, adaface_prompt_embs = \
            self.subj_basis_generator(id_prompt_embs, None, None, 
                                      out_id_embs_cfg_scale=self.out_id_embs_cfg_scale,
                                      is_face=True, is_training=False,
                                      adaface_prompt_embs_inf_type='full_half_pad')
        # adaface_subj_embs: [1, 1, 16, 768] -> [16, 768]
        adaface_subj_embs = adaface_subj_embs.squeeze(0).squeeze(0)
        if teacher_neg_id_prompt_embs is not None:
            # teacher_neg_id_prompt_embs: [1, 4, 768] -> [4, 768]
            teacher_neg_id_prompt_embs = teacher_neg_id_prompt_embs.squeeze(0)
        return adaface_subj_embs, teacher_neg_id_prompt_embs

class Arc2Face_ID2AdaPrompt(FaceID2AdaPrompt):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.clip_image_encoder = CLIPVisionModelWithMask.from_pretrained('openai/clip-vit-large-patch14')
        self.clip_preprocessor  = CLIPImageProcessor.from_pretrained('openai/clip-vit-large-patch14')
        self.clip_image_encoder.eval()
        if self.dtype == torch.float16:
            self.clip_image_encoder.half()
        print(f'CLIP image encoder loaded.')

        '''
        {'landmark_3d_68': <insightface.model_zoo.landmark.Landmark object at 0x7f8e3f0cc190>, 
        'landmark_2d_106': <insightface.model_zoo.landmark.Landmark object at 0x7f8e3f0cc2b0>, 
        'detection': <insightface.model_zoo.retinaface.RetinaFace object at 0x7f8e3f0cc100>, 
        'genderage': <insightface.model_zoo.attribute.Attribute object at 0x7f8e3f0cc1f0>, 
        'recognition': <insightface.model_zoo.arcface_onnx.ArcFaceONNX object at 0x7f8e3f0cc0d0>}
        '''
        # Use the same model as ID2AdaPrompt does.
        # FaceAnalysis will try to find the ckpt in: models/insightface/models/antelopev2. 
        # Note there's a second "model" in the path.        
        # Note DON'T use CUDAExecutionProvider, as it will hang DDP training. 
        # Seems when loading insightface onto the GPU, it will only reside on the first GPU. 
        # Then the process on the second GPU has issue to communicate with insightface on the first GPU, causing hanging.
        self.face_app = FaceAnalysis(name='antelopev2', root='models/insightface', 
                                            providers=['CPUExecutionProvider'])
        self.face_app.prepare(ctx_id=0, det_size=(512, 512))
        print(f'Face encoder loaded on CPU.')

        self.text_to_image_prompt_encoder = CLIPTextModelWrapper.from_pretrained(
                                                'models/arc2face', subfolder="encoder", 
                                                torch_dtype=self.dtype
                                            )
        self.tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14")

        if self.out_id_embs_cfg_scale == -1:
            self.out_id_embs_cfg_scale = 1
        # Arc2Face pipeline specific behaviors.
        self.gen_neg_img_prompt             = False
        self.use_clip_embs                  = False
        self.do_contrast_clip_embs          = False
        self.num_id_vecs                    = 16
        self.id_img_prompt_max_length       = 22
        self.clip_embedding_dim             = 1024
        self.name                           = 'arc2face'

        if self.adaface_ckpt_path is not None:
            self.load_adaface_ckpt(self.adaface_ckpt_path)

        print(f'Arc2Face text-to-ada prompt encoder initialized, number of ID vecs: {self.num_id_vecs}.')
    
    # Arc2Face_ID2AdaPrompt never uses clip_features or called_for_neg_img_prompt.
    def map_init_id_to_img_prompt_embs(self, init_id_embs, 
                                       clip_features=None,
                                       called_for_neg_img_prompt=False,
                                       return_full_and_core_embs=True):

        '''
        self.text_to_image_prompt_encoder: arc2face_models.py:CLIPTextModelWrapper instance.
        init_id_embs: (N, 512) normalized Face ID embeddings.
        return_full_and_core_embs: Return both the full prompt embeddings and the core embeddings. 
                                If False, return only the core embeddings.

        '''

        # arcface_token_id: 1014
        arcface_token_id = self.tokenizer.encode("id", add_special_tokens=False)[0]

        # This step should be quite fast, and there's no need to cache the input_ids.
        input_ids = self.tokenizer(
                "photo of a id person",
                truncation=True,
                padding="max_length",
                # In Arc2Face_ID2AdaPrompt, id_img_prompt_max_length is 22.
                # Arc2Face's image prompt is meanlingless in tokens other than ID tokens.
                max_length=self.id_img_prompt_max_length, 
                return_tensors="pt",
            ).input_ids.to(init_id_embs.device)
        # input_ids: [1, 22] or [3, 22] (during training).
        input_ids = input_ids.repeat(len(init_id_embs), 1)
        init_id_embs = init_id_embs.to(self.dtype)
        # face_embs_padded: [1, 512] -> [1, 768].
        face_embs_padded = F.pad(init_id_embs, (0, self.text_to_image_prompt_encoder.config.hidden_size - init_id_embs.shape[-1]), "constant", 0)
        # self.text_to_image_prompt_encoder(input_ids=input_ids, ...) is called twice. The first is only to get the token embeddings (the shallowest mapping).
        # The second call does the ordinary CLIP text encoding pass.
        token_embs = self.text_to_image_prompt_encoder(input_ids=input_ids, return_token_embs=True)
        token_embs[input_ids==arcface_token_id] = face_embs_padded

        prompt_embeds = self.text_to_image_prompt_encoder(
            input_ids=input_ids,
            input_token_embs=token_embs,
            return_token_embs=False
        )[0]

        # Restore the original dtype of prompt_embeds: float16 -> float32.
        prompt_embeds = prompt_embeds.to(self.dtype)

        if return_full_and_core_embs:
            # token 4: 'id' in "photo of a id person". 
            # 4:20 are the most important 16 embeddings that contain the subject's identity.
            # [N, 22, 768] -> [N, 16, 768]
            return prompt_embeds, prompt_embeds[:, 4:20]
        else:
            # [N, 16, 768]
            return prompt_embeds[:, 4:20]

    def get_id2img_learnable_modules(self):
        return [ self.text_to_image_prompt_encoder ]
    
# ConsistentID_ID2AdaPrompt is just a wrapper of ConsistentIDPipeline, so it's not an nn.Module.
class ConsistentID_ID2AdaPrompt(FaceID2AdaPrompt):
    def __init__(self, pipe=None, base_model_path=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if pipe is None:
            assert base_model_path is not None, "base_model_path should be provided."
            pipe = ConsistentIDPipeline.from_single_file(
                base_model_path, 
                torch_dtype=self.dtype
            )
            pipe.load_ConsistentID_model(consistentID_weight_path="./models/ConsistentID/ConsistentID-v1.bin",
                                         bise_net_weight_path="./models/ConsistentID/BiSeNet_pretrained_for_ConsistentID.pth")
            # Since pipe is None, this should be called during inference,
            # when the teacher ConsistentIDPipeline is not initialized. 
            # Therefore, we release VAE, UNet and text_encoder to save memory.
            pipe.release_components(["unet", "vae"])

        # Otherwise, we share the pipeline with the teacher. 
        # So we don't release the components.
        self.pipe                           = pipe
        self.face_app                       = pipe.face_app
        # ConsistentID uses 'laion/CLIP-ViT-H-14-laion2B-s32B-b79K'.
        self.clip_image_encoder             = patch_clip_image_encoder_with_mask(pipe.clip_encoder)
        self.clip_preprocessor              = pipe.clip_preprocessor
        self.text_to_image_prompt_encoder   = pipe.text_encoder
        self.tokenizer                      = pipe.tokenizer
        self.image_proj_model               = pipe.image_proj_model

        self.clip_image_encoder.eval()                
        self.image_proj_model.eval()
        if self.dtype == torch.float16:
            self.clip_image_encoder.half()
            self.image_proj_model.half()

        if self.out_id_embs_cfg_scale == -1:
            self.out_id_embs_cfg_scale      = 6
        # ConsistentIDPipeline specific behaviors.
        self.num_id_vecs                    = 8
        self.gen_neg_img_prompt             = True
        self.use_clip_embs                  = True
        self.do_contrast_clip_embs          = False
        self.clip_embedding_dim             = 1280
        self.s_scale                        = 1.0
        self.shortcut                       = False
        self.name                           = 'consistentID'

        if self.adaface_ckpt_path is not None:
            self.load_adaface_ckpt(self.adaface_ckpt_path)

        print(f'ConsistentID text-to-ada prompt encoder initialized, number of ID vecs: {self.num_id_vecs}.')

    def map_init_id_to_img_prompt_embs(self, init_id_embs, 
                                       clip_features=None,
                                       called_for_neg_img_prompt=False,
                                       return_full_and_core_embs=True):
        assert init_id_embs is not None, "init_id_embs should be provided."

        init_id_embs  = init_id_embs.to(self.dtype)
        clip_features = clip_features.to(self.dtype)

        if not called_for_neg_img_prompt:
            # clip_features: [BS, 514, 1280].
            # clip_features is provided when the function is called within 
            # ConsistentID_ID2AdaPrompt:extract_init_id_embeds_from_images(), which is
            # image_fg_features and image_bg_features concatenated at dim=1. 
            # Therefore, we split clip_image_double_embeds into image_fg_features and image_bg_features.
            # image_bg_features is not used in ConsistentID_ID2AdaPrompt.
            image_fg_features, image_bg_features = clip_features.chunk(2, dim=1)
            # clip_image_embeds: [BS, 257, 1280].
            clip_image_embeds = image_fg_features
        else:
            # clip_features is the negative image features. So we don't need to split it.
            clip_image_embeds = clip_features
            init_id_embs = torch.zeros_like(init_id_embs)

        faceid_embeds = init_id_embs
        # image_proj_model maps 1280-dim OpenCLIP embeddings to 768-dim face prompt embeddings.
        # clip_image_embeds are used as queries to transform faceid_embeds.
        # faceid_embeds -> kv, clip_image_embeds -> q
        if faceid_embeds.shape[0] != clip_image_embeds.shape[0]:
            breakpoint()

        global_id_embeds = self.image_proj_model(faceid_embeds, clip_image_embeds, shortcut=self.shortcut, scale=self.s_scale)
        
        if return_full_and_core_embs:
            # All ID prompt embeddings are core embeddings.
            return global_id_embeds, global_id_embeds
        else:
            return global_id_embeds

    def get_id2img_learnable_modules(self):
        return [ self.image_proj_model ]

def create_id2ada_prompt_encoder(adaface_encoder_type, adaface_ckpt_path=None, *args, **kwargs):
    if adaface_encoder_type == 'arc2face':
        id2ada_prompt_encoder = Arc2Face_ID2AdaPrompt(adaface_ckpt_path=adaface_ckpt_path, *args, **kwargs)
    elif adaface_encoder_type == 'consistentID':
        # The base_model_path is kind of arbitrary, as the UNet and VAE in the model will be released soon.
        # Only the consistentID modules and bise_net are used.
        id2ada_prompt_encoder = ConsistentID_ID2AdaPrompt(
                                        base_model_path="models/ensemble/sd15-dste8-vae.safetensors",
                                        adaface_ckpt_path=adaface_ckpt_path, *args, **kwargs)
    else:
        breakpoint()
    
    return id2ada_prompt_encoder

'''
# For ip-adapter distillation on objects. Strictly speaking, it's not face-to-image prompts, but
# CLIP/DINO visual features to image prompts.
class Objects_Vis2ImgPrompt(nn.Module):
    def __init__(self):
        self.dino_encoder = ViTModel.from_pretrained('facebook/dino-vits16')
        self.dino_encoder.eval()
        self.dino_encoder.half()
        self.dino_preprocess = ViTFeatureExtractor.from_pretrained('facebook/dino-vits16')
        print(f'DINO encoder loaded.')

'''
