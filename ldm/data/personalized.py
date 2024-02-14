import os
import numpy as np
import PIL
from PIL import Image
from torch.utils.data import Dataset, Sampler
from torchvision import transforms
from transformers import CLIPTokenizer, AutoProcessor
from torchvision.transforms import InterpolationMode
from .compositions import sample_compositions
import random
import torch
import regex as re
import webdataset as wds
import glob
from evaluation.eval_utils import parse_subject_file
from ldm.modules.subj_basis_generator import CLIPVisionModelWithMask

imagenet_templates_smallest = [
    'a photo of a {}',
]

imagenet_templates_small = [
    'a photo of a {}',
    'a rendering of a {}',
    'a cropped photo of the {}',
    'the photo of a {}',
    'a photo of a clean {}',
    'a photo of a dirty {}',
    'a dark photo of the {}',
    'a photo of my {}',
    'a photo of the cool {}',
    'a close-up photo of a {}',
    'a bright photo of the {}',
    'a cropped photo of a {}',
    'a photo of the {}',
    'a good photo of the {}',
    'a photo of one {}',
    'a close-up photo of the {}',
    'a rendition of the {}',
    'a photo of the clean {}',
    'a rendition of a {}',
    'a photo of a nice {}',
    'a good photo of a {}',
    'a photo of the nice {}',
    'a photo of the small {}',
    'a photo of the weird {}',
    'a photo of the large {}',
    'a photo of a cool {}',
    'a photo of a small {}',
    'an illustration of a {}',
    'a rendering of a {}',
    'a cropped photo of the {}',
    'the photo of a {}',
    'an illustration of a clean {}',
    'an illustration of a dirty {}',
    'a dark photo of the {}',
    'an illustration of my {}',
    'an illustration of the cool {}',
    'a close-up photo of a {}',
    'a bright photo of the {}',
    'a cropped photo of a {}',
    'an illustration of the {}',
    'a good photo of the {}',
    'an illustration of one {}',
    'a close-up photo of the {}',
    'a rendition of the {}',
    'an illustration of the clean {}',
    'a rendition of a {}',
    'an illustration of a nice {}',
    'a good photo of a {}',
    'an illustration of the nice {}',
    'an illustration of the small {}',
    'an illustration of the weird {}',
    'an illustration of the large {}',
    'an illustration of a cool {}',
    'an illustration of a small {}',
    'a depiction of a {}',
    'a rendering of a {}',
    'a cropped photo of the {}',
    'the photo of a {}',
    'a depiction of a clean {}',
    'a depiction of a dirty {}',
    'a dark photo of the {}',
    'a depiction of my {}',
    'a depiction of the cool {}',
    'a close-up photo of a {}',
    'a bright photo of the {}',
    'a cropped photo of a {}',
    'a depiction of the {}',
    'a good photo of the {}',
    'a depiction of one {}',
    'a close-up photo of the {}',
    'a rendition of the {}',
    'a depiction of the clean {}',
    'a rendition of a {}',
    'a depiction of a nice {}',
    'a good photo of a {}',
    'a depiction of the nice {}',
    'a depiction of the small {}',
    'a depiction of the weird {}',
    'a depiction of the large {}',
    'a depiction of a cool {}',
    'a depiction of a small {}',
]

per_img_token_list = [
    'א', 'ב', 'ג', 'ד', 'ה', 'ו', 'ז', 'ח', 'ט', 'י', 'כ', 'ל', 'מ', 'נ', 'ס', 'ע', 'פ', 'צ', 'ק', 'ר', 'ש', 'ת',
]

# "bike", "person", "ball" are a commonly seen "object" that can appear in various contexts. 
# So it's a good template object for computing the delta loss.
# "person" is also used for animals, as their dynamic compositions are highly similar.
# "mickey", "snoopy", "pikachu" are common cartoon characters.
# These three words are for 3 broad_classes: 0, 1, 2.
default_cls_delta_strings = [ "ball", "person", "mickey" ]

single_human_pat = "man|woman|person|boy|girl|child|kid|baby|adult|guy|lady|gentleman|lady|male|female|human"
single_role_pat  = "cook|chef|waiter|waitress|doctor|nurse|policeman|policewoman|fireman|firewoman|firefighter|teacher|student|professor|driver|pilot|farmer|worker|artist|painter|photographer|dancer|singer|musician|player|athlete|player|biker|cyclist|bicyclist"
plural_human_pat = "men|women|people|boys|girls|children|kids|babies|adults|guys|ladies|gentlemen|ladies|males|females|humans"
plural_role_pat  = "cooks|chefs|waiters|waitresses|doctors|nurses|policemen|policewomen|firemen|firewomen|firefighters|teachers|students|professors|drivers|pilots|farmers|workers|artists|painters|photographers|dancers|singers|musicians|players|athletes|players|bikers|cyclists|bicyclists"
animal_pat       = "cat|cats|dog|dogs"
human_animal_pat = "|".join([single_human_pat, single_role_pat, plural_human_pat, plural_role_pat, animal_pat])

class PersonalizedBase(Dataset):
    def __init__(self,
                 data_roots,    # a list of strings, each string is a path to a folder containing images.
                 size=None,
                 repeats=100,
                 flip_p=0.5,
                 # rand_scale_range: None (disabled) or a tuple of floats
                 # that specifies the (minimum, maximum) scaling factors.
                 rand_scale_range=None,
                 set="train",
                 subject_string="z",
                 background_string="y",
                 wds_background_string="w",
                 # placeholder_prefix for all types of prompts. Could be a list of strings, separated by ",".
                 common_placeholder_prefix=None,   
                 # placeholder_prefix for compositional prompts. Could be a list of strings, separated by ",".
                 compos_placeholder_prefix=None,   
                 # cls string used to compute the delta loss.
                 cls_delta_string=None,  
                 #cls_uses_subj_init_words=False,
                 # background strings used to compute the delta loss.
                 cls_bg_delta_string=None,
                # num_vectors_per_token: how many vectors in each layer are allocated to model 
                # the subject. If num_vectors_per_token > 1, pad with "," in the prompts to leave
                # room for those extra vectors.
                 num_vectors_per_token=1,
                 num_vectors_per_bg_token=1,
                 center_crop=False,
                 num_compositions_per_image=1,
                 broad_class=1,
                 # If data_roots contain multiple folders, a subject each folder, 
                 # then subj_info_filepaths should point to a list of subject info files 
                 # containing the cls_delta_string of all subjects, in "init_strings".
                 # cls_bg_delta_string is optional, and can be specified in "all_bg_init_words".
                 subj_info_filepaths=None,
                 ext_image_features=False,
                 wds_comp_db_path=None,    # Path to the composition webdatabase .tar file
                 verbose=False,
                 ):

        # Expand wildcards in data_roots.
        if isinstance(data_roots, str):
            data_roots = [ data_roots ]
        data_roots_expanded = [ glob.glob(data_root) for data_root in data_roots ]
        # Sort the data_roots, so that the order of subjects is consistent.
        self.data_roots = sorted(sum(data_roots_expanded, []))
        # Remove non-directories.
        self.data_roots = list(filter(lambda x: os.path.isdir(x), self.data_roots))

        # subject_names: sorted ascendingly for subjects within the same folder.
        self.subject_names = [ os.path.basename(data_root) for data_root in self.data_roots ]

        assert len(self.data_roots) > 0, f"No data found in data_roots={data_roots}!"

        self.image_paths_by_subj    = []
        self.fg_mask_paths_by_subj  = []
        self.caption_paths_by_subj  = []
        self.feat_paths_by_subj     = []

        for data_root in self.data_roots:
            # image_paths and mask_paths are full paths.
            all_file_paths      = [os.path.join(data_root, file_path) for file_path in sorted(os.listdir(data_root))]
            image_paths         = list(filter(lambda x: "_mask" not in x and os.path.splitext(x)[1].lower() != '.txt', all_file_paths))
            fg_mask_paths       = [ os.path.splitext(x)[0] + "_mask.png" for x in image_paths ]
            fg_mask_paths       = list(map(lambda x: x if x in all_file_paths else None, fg_mask_paths))
            num_valid_fg_masks  = sum([ 1 if x is not None else 0 for x in fg_mask_paths ])
            caption_paths       = [ os.path.splitext(x)[0] + ".txt" for x in image_paths ]
            caption_paths       = list(map(lambda x: x if x in all_file_paths else None, caption_paths))
            num_valid_captions  = sum([ 1 if x is not None else 0 for x in caption_paths ])
            feat_paths          = [ os.path.splitext(x)[0] + ".npy" for x in image_paths ]
            # Don't filter feat_paths. If the .npy file is missing, then it'll be created at the first loading.
            # feat_paths          = list(map(lambda x: x if x in all_file_paths else None, feat_paths))
            num_valid_feats     = sum([ 1 if x is not None else 0 for x in feat_paths ])

            self.image_paths_by_subj.append(image_paths)
            self.fg_mask_paths_by_subj.append(fg_mask_paths)
            self.caption_paths_by_subj.append(caption_paths)
            self.feat_paths_by_subj.append(feat_paths)

            if verbose:
                print("{} images, {} fg masks, {} captions found in '{}'".format( \
                    len(image_paths), num_valid_fg_masks, num_valid_captions, data_root))
                if num_valid_fg_masks > 0 and num_valid_fg_masks < len(image_paths):
                    print("WARNING: {} fg masks are missing!".format(len(image_paths) - num_valid_fg_masks))
                if num_valid_captions > 0 and num_valid_captions < len(image_paths):
                    print("WARNING: {} captions are missing!".format(len(image_paths) - num_valid_captions))
                if num_valid_feats > 0 and num_valid_feats < len(image_paths):
                    print("WARNING: {} feature files are missing!".format(len(image_paths) - num_valid_feats))

        self.num_subjects = len(self.subject_names)
        # self.image_paths, ...         are for the one-level indexing, i.e., directly indexing into a particular image.
        # self.image_paths_by_subj, ... are for the two-level indexing, i.e., first indexing into a subject, then
        # indexing into an image within that subject.
        self.image_paths   = sum(self.image_paths_by_subj, [])
        self.fg_mask_paths = sum(self.fg_mask_paths_by_subj, [])
        self.caption_paths = sum(self.caption_paths_by_subj, [])
        self.feat_paths    = sum(self.feat_paths_by_subj, [])

        self.num_images = len(self.image_paths)
        if set == "train":
            self.is_training = True
            self._length = self.num_images * repeats
        else:
            self.is_training = False
            self._length = self.num_images 

        self.wds_comp_db_path = wds_comp_db_path
        # wds composition is enabled if there are fg masks.
        if self.is_training and (self.wds_comp_db_path is not None) and (num_valid_fg_masks > 0):
            self.comp_wds = wds.WebDataset(self.wds_comp_db_path).shuffle(100).decode("pil").to_tuple("jpg;png", "json")
            self.comp_wds_iter = iter(self.comp_wds)
            self.do_wds_comp = True
            print(f"Composition webdataset {self.wds_comp_db_path} is enabled")
        else:
            self.comp_wds = None
            self.comp_wds_iter = None
            self.do_wds_comp = False

        subj2attr = {}
        if subj_info_filepaths is not None:
            for subj_info_filepath in subj_info_filepaths:
                _, subj2attr_singlefile = parse_subject_file(subj_info_filepath)
                # Make sure subject names are always unique across different files.
                for k in subj2attr_singlefile:
                    if k not in subj2attr:
                        subj2attr[k] = subj2attr_singlefile[k]
                    else:
                        # Make sure the keys are unique across different files.
                        # If not, then the keys are duplicated, and we need to fix the data files.
                        for subj_name in subj2attr_singlefile[k]:
                            assert subj_name not in subj2attr[k], f"Duplicate subject {k} found in {subj_info_filepaths}!"
                            subj2attr[k][subj_name] = subj2attr_singlefile[k][subj_name]

        if 'broad_classes' not in subj2attr:
            self.broad_classes          = [ broad_class ] * self.num_subjects
        else:
            self.broad_classes          = [ subj2attr['broad_classes'][subject_name] \
                                            for subject_name in self.subject_names ]
        # cartoon characters are usually depicted as human-like, so is_animal is True.
        self.are_animals = [ (broad_class == 1 or broad_class == 2) \
                              for broad_class in self.broad_classes ]

        if self.num_subjects == 1:
            self.subject_strings        = [ subject_string ]
            self.background_strings     = [ background_string ]
            self.wds_background_strings = [ wds_background_string ]
        else:
            # For multiple subjects, the subject_string is like: 'z01', 'z02', ....
            # Avoid using z1 and z11, ..., in case the tokenizer wrongly segments z11 as z1 and 1 (probably won't happen for CLIP
            # tokenizer, but just to be safe.)
            self.subject_strings        = [ subject_string          + f"{i+1:02}" for i in range(self.num_subjects) ]
            # For multiple subjects, the background_string is like: 'y01', 'y02', ....
            # Don't share the background_string, since the background of different subject images
            # has different distributions.
            self.background_strings     = [ background_string       + f"{i+1:02}" for i in range(self.num_subjects) ]
            # For multiple subjects, the wds_background_string is like: 'w01', 'w02', ....
            # Don't share the wds_background_string, since the background of different subject images
            # has different distributions.
            self.wds_background_strings = [ wds_background_string   + f"{i+1:02}" for i in range(self.num_subjects) ]
            
        self.tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14")
        # subject_token, background_token: subject_string and background_string converted to 
        # token numbers.
        self.subject_tokens     = [ self.tokenizer(subject_string)['input_ids'][1] \
                                    for subject_string in self.subject_strings ]
        self.background_tokens  = [ self.tokenizer(background_string)['input_ids'][1] \
                                    for background_string in self.background_strings ]

        self.ext_image_features = ext_image_features
        if self.ext_image_features:
            self.clip_image_encoder = CLIPVisionModelWithMask.from_pretrained("laion/CLIP-ViT-H-14-laion2B-s32B-b79K")
            self.clip_preprocessor  = AutoProcessor.from_pretrained("laion/CLIP-ViT-H-14-laion2B-s32B-b79K")
        else:
            self.clip_image_encoder = None
            self.clip_preprocessor  = None

        # placeholder_prefix could be a list of strings, separated by ",".
        if common_placeholder_prefix is not None:
            self.common_placeholder_prefixes   = re.split("\s*,\s*", common_placeholder_prefix)
        else:
            self.common_placeholder_prefixes   = None
        if compos_placeholder_prefix is not None:
            self.compos_placeholder_prefixes   = re.split("\s*,\s*", compos_placeholder_prefix)
        else:
            self.compos_placeholder_prefixes   = None

        if 'init_strings' in subj2attr:
            self.cls_delta_strings = [ subj2attr['init_strings'][subject_name] \
                                            for subject_name in self.subject_names ]
        else:
            if cls_delta_string is not None:
                self.cls_delta_strings = [ cls_delta_string ] * self.num_subjects
            else:
                self.cls_delta_strings = [ default_cls_delta_strings[broad_class] \
                                            for broad_class in self.broad_classes ]

        if 'all_bg_init_words' in subj2attr:
            self.cls_bg_delta_strings = [ subj2attr['all_bg_init_words'][subject_name] \
                                            for subject_name in self.subject_names ]
        else:                
            if cls_bg_delta_string is not None:
                cls_bg_delta_string = re.split(r"\s+", cls_bg_delta_string) 
            else:
                cls_bg_delta_string = [ 'unknown' ]
            # All subjects use the same cls_bg_delta_string.
            self.cls_bg_delta_strings = cls_bg_delta_string * self.num_subjects

        # list_initializer_weights are not used in the data loader, but are used to initialize
        # the embedding manager.
        if 'all_init_word_weights' in subj2attr:
            self.list_initializer_weights = [ subj2attr['all_init_word_weights'][subject_name] \
                                              for subject_name in self.subject_names ]
        else:
            self.list_initializer_weights = [ None ] * self.num_subjects
        # bg_initializer_weights are always None (uniform over bg initializer words).
        self.list_bg_initializer_weights = [ None ] * self.num_subjects

        self.num_vectors_per_token    = num_vectors_per_token
        self.num_vectors_per_bg_token = num_vectors_per_bg_token
        self.center_crop = center_crop

        self.size = size
        interpolation_scheme = "nearest"
        self.interpolation = { "nearest":  PIL.Image.NEAREST,
                               "bilinear": PIL.Image.BILINEAR,
                               "bicubic":  PIL.Image.BICUBIC,
                               "lanczos":  PIL.Image.LANCZOS,
                              }[interpolation_scheme]
        
        if self.is_training:
            self.flip = transforms.RandomHorizontalFlip(p=flip_p)
            if rand_scale_range is not None:
                # If the scale factor > 1, RandomAffine will crop the scaled image to the original size.
                # If the scale factor < 1, RandomAffine will pad the scaled image to the original size.
                # Because the scaled "images" are extended with two mask channels, which contain many zeros,
                # We have to use interpolation=InterpolationMode.NEAREST, otherwise the zeros in the mask will 
                # be interpolated to image pixel values.
                # RandomAffine() doesn't change the input image size, 
                # so transforms.Resize() is redundant. Anyway, just keep it here.
                self.random_scaler = transforms.Compose([
                                        transforms.RandomAffine(degrees=0, shear=0, scale=rand_scale_range,
                                                                interpolation=InterpolationMode.NEAREST),
                                        transforms.Resize(size, interpolation=InterpolationMode.NEAREST),
                                     ])
                print(f"{set} images will be randomly scaled in range {rand_scale_range}")

            if self.do_wds_comp:
                # rand_scale_range is (0.7, 1.0) by default. Here we use a smaller range, 
                # i.e., more aggressive scaling.
                print(f"{set} fg will be randomly scaled to (0.5, 0.8) before overlaying to bg images")
                self.resize_and_crop = transforms.Compose([
                                            transforms.Resize(size, interpolation=InterpolationMode.NEAREST),
                                            transforms.CenterCrop(size),
                                        ])
        else:
            self.random_scaler = None
            self.flip = None

        self.num_compositions_per_image = num_compositions_per_image
   
    def __len__(self):
        return self._length

    def __getitem__(self, index):
        is_subject_idx = False
        if isinstance(index, tuple):
            index, is_subject_idx = index

        example = {}
        if is_subject_idx:
            image_paths     = self.image_paths_by_subj[index]
            # Draw a random image from the subject dataset indexed by index.
            image_idx       = random.randint(0, len(image_paths) - 1)

            image_path      = image_paths[image_idx]
            fg_mask_path    = self.fg_mask_paths_by_subj[index][image_idx]
            caption_path    = self.caption_paths_by_subj[index][image_idx]
            feat_path       = self.feat_paths_by_subj[index][image_idx]
            subject_idx     = index
        else:
            image_path    = self.image_paths[index % self.num_images]
            fg_mask_path  = self.fg_mask_paths[index % self.num_images] 
            caption_path  = self.caption_paths[index % self.num_images]
            feat_path     = self.feat_paths[index % self.num_images]
            subject_idx   = 0

        cls_delta_string      = self.cls_delta_strings[subject_idx]
        wds_background_string = self.wds_background_strings[subject_idx]
        # subject_token, background_token: subject_string and background_string converted to 
        # token numbers.        
        subject_token         = self.subject_tokens[subject_idx]
        background_token      = self.background_tokens[subject_idx]

        image_obj = Image.open(image_path)
        if not image_obj.mode == "RGB":
            image_obj = image_obj.convert("RGB")

        # default to score-sde preprocessing -- i don't understand what this comment means, but keep it here. 
        image       = np.array(image_obj).astype(np.uint8)
        # Convert back to Image object, so that image_obj is made sure to be uint8.
        image_obj   = Image.fromarray(image)
       
        if fg_mask_path is not None:
            # mask is 8-bit grayscale, with same size as image. E.g., image is of [1282, 1282, 3],
            # then mask is of [1282, 1282], with values True or False. 
            # After converting to "L", mask is still [1282, 1282],
            # but pixel values change from True (1) to 255.
            fg_mask_obj = Image.open(fg_mask_path).convert("L")
            has_fg_mask = True
        else:
            # This mask is created to avoid multiple None-checking later. But it's not passed to the trainer.
            # image_obj has been converted to "RGB" if it doesn't have 3 channels. 
            # So mask is of [1282, 1282, 3] as well.
            # To conform with the case where fg_mask_path is not None, we make the fg_mask 
            # pixel values to be either 0 or 255.
            fg_mask_obj = Image.fromarray(np.ones_like(image[:, :, 0]) * 255)
            has_fg_mask = False

        # image is made sure to be uint8. So fg_mask is also uint8.
        fg_mask         = np.array(fg_mask_obj).astype(np.uint8)
        # Concatenate fg_mask to the last channel of image, so that we don't need to transform fg_mask separately.
        # image_mask: (1282, 1282, 4)
        image_mask      = np.concatenate([image, fg_mask[:, :, np.newaxis]], axis=2)
        image_mask_obj  = Image.fromarray(image_mask)

        #mask_fg_percent = fg_mask.astype(float).sum() / (255 * fg_mask.size)
        # print(f"mask_fg_percent: {mask_fg_percent}")

        #print(f"subj_prompt_comp: {subj_prompt_comp}")
        #print(f"cls_prompt_comp: {cls_prompt_comp}")

        if self.center_crop:        # default: False
            crop = min(image.shape[0], image.shape[1])
            h, w, = image.shape[0], image.shape[1]
            image_mask = image_mask[(h - crop) // 2:(h + crop) // 2,
                         (w - crop) // 2:(w + crop) // 2]
            
            image_mask_obj  = Image.fromarray(image_mask)

        if self.size is not None:
            # Because image_mask_obj has an extra channel of mask, which contains many zeros.
            # Using resampling schemes other than 'NEAREST' will introduce many zeros at the border. 
            # Therefore, we fix the resampling/interpolation scheme to be 'NEAREST'.
            #image_mask_obj_old = image_mask_obj
            image_mask_obj = image_mask_obj.resize((self.size, self.size), resample=self.interpolation)
            #breakpoint()

        if self.flip:
            image_mask_obj = self.flip(image_mask_obj)
        
        image_mask = np.array(image_mask_obj)

        if has_fg_mask and self.do_wds_comp:
            gen_wds_comp = True
            # If do_wds_comp, and fg areas are large enough, then we do more aggressive scaling to fg,
            # so that fg won't dominate the whole image, which may help learning composition.
        else:
            gen_wds_comp = False

        if self.random_scaler is not None:
            scale_p = 1
        else:
            scale_p = 0
        shift_p = 1

        # Do random scaling with 50% chance. Not to do it all the time, 
        # as it seems to hurt (maybe introduced domain gap between training and inference?)
        if scale_p > 0 and random.random() < scale_p:
                image_tensor = torch.tensor(image_mask).permute(2, 0, 1)
                # aug_mask doesn't have to take {0, 255}. Since some inaccuracy around the boundary
                # doesn't really matter. But fg_mask has to take {0, 255}, otherwise after scaling,
                # some foreground pixels will become 0, and when the foreground area is small, 
                # such loss is significant.
                aug_mask    = torch.ones_like(image_tensor[0:1])
                image_ext   = torch.cat([image_tensor, aug_mask], dim=0)
                # image_ext: [4, 512, 512]
                image_ext   = self.random_scaler(image_ext)
                # After random scaling, the valid area is only a sub-region at the center of the image.
                # NOTE: random shifting DISABLED, as it seems to hurt.
                # ??% chance to randomly roll towards right and bottom (), 
                # and ??% chance to keep the valid area at the center.
                if random.random() < shift_p:
                    # count number of empty lines at the left, right, top, bottom edges of image_ext.
                    # aug_mask = image_ext[3] is uint8, so all pixels >= 0. 
                    # sum(dim=1).cumsum() == 0 means this is an empty row at the top of the image.
                    top0     = (image_ext[3].sum(dim=1).cumsum(dim=0) == 0).sum().item()
                    # flip first then cumsum(), so that cumsum() is from bottom to top.
                    bottom0  = (image_ext[3].sum(dim=1).flip(dims=(0,)).cumsum(dim=0) == 0).sum().item()
                    # sum(dim=0).cumsum() == 0 means this is an empty column at the left of the image.
                    left0    = (image_ext[3].sum(dim=0).cumsum(dim=0) == 0).sum().item()
                    # flip first then cumsum(), so that cumsum() is from right to left.
                    right0   = (image_ext[3].sum(dim=0).flip(dims=(0,)).cumsum(dim=0) == 0).sum().item()
                    # Randomly roll towards right and bottom, within the empty edges.
                    # randint() includes both end points, so max(dy) =  top0 + bottom.
                    # MG: margin at each side, i.e., after rolling, 
                    # there are still at least 12 empty lines at each side.
                    # The average scaling is (1+0.7)/2 = 0.85, i.e., 7.5% empty lines at each side.
                    # 7.5% * 512 = 38.4 pixels, so set the margin to be around 1/3 of that.
                    MG = 12     
                    if top0 + bottom0 > 2*MG:
                        dy = random.randint(0, top0 + bottom0 - 2*MG)
                        # Shift up instead (negative dy)
                        if dy > bottom0 - MG:
                            dy = -(dy - bottom0 + MG)
                    else:
                        dy = 0
                    if left0 + right0 > 2*MG:
                        dx = random.randint(0, left0 + right0 - 2*MG)
                        # Shift left instead (negative dx)
                        if dx > right0 - MG:
                            dx = -(dx - right0 + MG)
                    else:
                        dx = 0
                    # Because the image border is padded with zeros, we can simply roll() 
                    # without worrying about the border values.
                    image_ext = torch.roll(image_ext, shifts=(dy, dx), dims=(1, 2))

                # image_mask is a concatenation of image and mask, not a mask for the image.
                image_mask  = image_ext[:4].permute(1, 2, 0).numpy().astype(np.uint8)
                # aug_mask: [512, 512].
                aug_mask    = image_ext[4].numpy().astype(np.uint8)

                # Sanity check.
                if np.any(image_mask * aug_mask[:, :, np.newaxis] != image_mask):
                    breakpoint()
        else:
            # If random scaling or wds composition is enabled, then even if no scaling happens
            # or no wds_image_mask is generated, we still need to put a all-1 'aug_mask' into the example.
            # 'aug_mask' has to be present in all examples, otherwise collation will encounter exceptions.
            if self.random_scaler or self.do_wds_comp:
                aug_mask = np.ones_like(image_mask[:, :, 0])
            else:
                # aug_mask will not be present in any examples, so set it to None.
                aug_mask = None

        image       = image_mask[:, :, :3]
        # fg_mask is a 1-channel mask.
        fg_mask     = image_mask[:, :, 3]
        # Scale and round fg_mask to 0 or 1.
        fg_mask     = (fg_mask  / 255).astype(np.uint8)
        # No need to scale aug_mask, as it's already 0 or 1.
        if aug_mask is not None:
            aug_mask    = aug_mask.astype(np.uint8)

        example["image_path"]   = image_path
        example["has_fg_mask"]  = has_fg_mask
        # If no fg_mask is loaded from file. 'fg_mask' is all-1, and 'has_fg_mask' is set to False.
        # 'fg_mask' has to be present in all examples, otherwise collation will cause exceptions.
        example["fg_mask"]      = fg_mask
        example["aug_mask"]     = aug_mask

        # Also return the unnormalized numpy array image.
        # example["image_unnorm"]: [0, 255]
        example["image_unnorm"] = image
        # example["image"]: [0, 255] -> [-1, 1]
        example["image"]        = (image / 127.5 - 1.0).astype(np.float32)
        
        cache_image_features = False
        if self.ext_image_features:
            if cache_image_features and os.path.exists(feat_path):
                # image_features: [1, 514, 1280]
                image_features = torch.load(feat_path)
            else:
                # First time processing the image, so we need to extract the image features.
                # input to clip_preprocessor: an image or a batch of images, each being PIL.Image.Image, numpy.ndarray, 
                # torch.Tensor, tf.Tensor or jax.ndarray.
                # image_pixel_values: [1, 3, 224, 224]
                image_pixel_values = self.clip_preprocessor(images=image, return_tensors="pt").pixel_values
                # fg_mask2: [1, 1, 512, 512]
                fg_mask2 = torch.tensor(fg_mask).unsqueeze(0).float()
                with torch.no_grad():
                    # image_fg_features: [1, 257, 1280]. 257: 16*16 (patch_embeds) + 1 (class_embeds).
                    image_fg_features  = self.clip_image_encoder(image_pixel_values, attn_mask=fg_mask2, output_hidden_states=True).hidden_states[-2]
                    # A negative mask is used to extract the background features.
                    image_bg_features  = self.clip_image_encoder(image_pixel_values, attn_mask=1-fg_mask2, output_hidden_states=True).hidden_states[-2]

                # image_features: [1, 514, 1280] -> [514, 1280].
                # Remove the batch dimension before being collated into a batch.
                image_features    = torch.cat([image_fg_features, image_bg_features], dim=1).squeeze(0)
                if cache_image_features:
                    # Save the image features to disk, so that we don't need to extract them again.
                    torch.save(image_features, feat_path)
                    print(f"Extracted image features for {image_path}, saved to {feat_path}")

            example["image_features"] = image_features

        if gen_wds_comp:
            Found = False
            while not Found:
                try:
                    bg_img, bg_json = next(self.comp_wds_iter)
                except:
                    self.comp_wds_iter = iter(self.comp_wds)
                    bg_img, bg_json = next(self.comp_wds_iter)

                bg_prompt = bg_json['caption'].lower()
                # Skip too short prompts.
                if len(bg_prompt.strip()) < 5:
                    continue
                bg_prompt_tokens = self.tokenizer(bg_prompt)['input_ids']
                # Skip those image/prompt pairs that will cause parsing errors.
                contains_special_token = subject_token       in bg_prompt_tokens \
                                         or background_token in bg_prompt_tokens \
                                         or (wds_background_string is not None \
                                             and wds_background_string in bg_prompt_tokens)
                
                if re.search(human_animal_pat, bg_prompt):
                    contains_human = True
                else:
                    contains_human = False

                hw_ratio = bg_json['width'] / bg_json['height']
                if hw_ratio >= 1.34 and hw_ratio < 0.75:
                    is_bad_size = True
                else:
                    is_bad_size = False
                
                orig_h, orig_w = bg_json['original_height'], bg_json['original_width']
                # Here we use max() instead of min() as below, to filter on the smaller dimension.
                edge_ratio = max(self.size / orig_h, self.size / orig_w)
                # edge_ratio is the ratio between the target 512x512 image and the shorter edge of the original image.
                # If it's too large, it means the original image is too small, and we skip it.
                if edge_ratio >= 1.3:
                    is_too_small = True
                else:
                    is_too_small = False

                # Skip wds image/prompt pairs that contain humans, special tokens or are of bad size.
                Found = not contains_special_token and not contains_human \
                         and not is_bad_size and not is_too_small

            # bg_img is PIL Image -> np.array (512, 512, 3)
            bg_img = np.array(bg_img).astype(np.uint8)
            orig_h, orig_w = bg_json['original_height'], bg_json['original_width']
            min_height, min_width = self.size, self.size
            scale = min(min_height / orig_h, min_width / orig_w)
            bg_h, bg_w   = int(orig_h * scale), int(orig_w * scale)

            if bg_h < min_height:
                h_pad_top = int((min_height - bg_h) / 2.0)
            else:
                h_pad_top = 0

            if bg_w < min_width:
                w_pad_left = int((min_width - bg_w) / 2.0)
            else:
                w_pad_left = 0

            # Remove the padding from the original bg image. 
            # bg_image_nopad: [bg_h, bg_w, 3]
            bg_image_nopad = bg_img[h_pad_top:h_pad_top+bg_h, w_pad_left:w_pad_left+bg_w, :]
            # Resize and crop to 512x512.
            bg_image_512_obj = self.resize_and_crop(Image.fromarray(bg_image_nopad))
            # Convert back to numpy array.
            bg_image_512 = np.array(bg_image_512_obj).astype(np.uint8)

            # Blend fg area with bg_img. fg_mask is 2D, so add 1D channel.
            wds_image    = np.where(fg_mask[:, :, None] > 0, image, bg_image_512)

        self.generate_prompts(example, subject_idx)

        if gen_wds_comp:
            # common_placeholder_prefix is prepended to caption and caption_bg.
            # compos_placeholder_prefix is prepended to subj_prompt_single, subj_prompt_comps,
            # cls_prompt_single, cls_prompt_comps, which we don't need to change, as they are 
            # for compositional distillation.
            wds_comp_extra      = ", in front of " + bg_prompt
            wds_cls_comp_extra  = " " + cls_delta_string + wds_comp_extra
            example["wds_comp_extra"]       = wds_comp_extra
            example["wds_cls_comp_extra"]   = wds_cls_comp_extra
            example["wds_caption"]          = example["caption"]    + wds_comp_extra
            example["wds_cls_caption"]      = example["caption"]    + wds_cls_comp_extra
            example["wds_caption_bg"]       = self.repl_bg_as_wbg(example["caption_bg"], subject_idx) + wds_comp_extra
            example["wds_cls_caption_bg"]   = self.repl_bg_as_wbg(example["caption_bg"], subject_idx) + wds_cls_comp_extra
            example["wds_image"]            = (wds_image    / 127.5 - 1.0).astype(np.float32)
            example["wds_image_bgonly"]     = (bg_image_512 / 127.5 - 1.0).astype(np.float32)
            # fg_mask of wds_image is the same as non-wds instances. So no need to assign.
            example["wds_aug_mask"]         = aug_mask
        else:
            example["wds_comp_extra"]       = ""
            example["wds_cls_comp_extra"]   = ""
            example["wds_caption"]          = example["caption"]
            example["wds_caption_bg"]       = example["caption_bg"]
            example["wds_image"]            = example["image"]
            example["wds_aug_mask"]         = example["aug_mask"]
            # No wds_cls_caption, wds_cls_caption_bg, wds_image_bgonly. 
            # They are only accessed when 'has_wds_comp' is True.

        example["has_wds_comp"]         = gen_wds_comp

        DEBUG_WDS = False
        if DEBUG_WDS and gen_wds_comp:
            self.wds_sample_dir = "wds-samples"
            os.makedirs(self.wds_sample_dir, exist_ok=True)
            wds_sample_count = len(os.listdir(self.wds_sample_dir))
            wds_sample_image_filepath = os.path.join(self.wds_sample_dir, f'{wds_sample_count:04}.jpg')
            if os.path.exists(wds_sample_image_filepath):
                while os.path.exists(wds_sample_image_filepath):
                    wds_sample_count += 1
                    wds_sample_image_filepath   = os.path.join(self.wds_sample_dir, f'{wds_sample_count:04}.jpg')
            
            wds_sample_caption_filepath = os.path.join(self.wds_sample_dir, f'{wds_sample_count:04}.txt')
            # Overlay wds_aug_mask on wds_image.
            # Create a pure red image
            red_image  = np.ones_like(wds_image) * 255
            red_image[:, :, 1:] = 0
            green_image = np.ones_like(wds_image) * 255
            green_image[:, :, [0,2]] = 0

            if random.random() < 0:
                wds_image2 = wds_image * 0.9 \
                            + aug_mask[:, :, None] * green_image * 0.1 \
                            + fg_mask[:, :, None]  * red_image   * 0.3
                wds_image2 = np.clip(wds_image2, 0, 255).astype(np.uint8)
            else:
                wds_image2 = wds_image

            Image.fromarray(wds_image2).save(wds_sample_image_filepath)
            print("Saved wds sample to {}".format(wds_sample_image_filepath))
            wds_sample_caption = example['cls_prompt_single'] + wds_comp_extra
            with open(wds_sample_caption_filepath, 'w') as f:
                f.write(wds_sample_caption)

        return example

    def generate_prompts(self, example, subject_idx):
        # If there are multiple subjects, then subject_string is like: 'z0', 'z1', ....
        # the background_string is like: 'y0', 'y1', ....
        # Otherwise, subject_string is simply 'z', and background_string is simply 'y'.
        subject_string      = self.subject_strings[subject_idx]
        background_string   = self.background_strings[subject_idx]        
        cls_delta_string    = self.cls_delta_strings[subject_idx]
        broad_class         = self.broad_classes[subject_idx]
        is_animal           = self.are_animals[subject_idx]
        # If background_string is specified, cls_bg_delta_string should always be specified 
        # in the commmand line (passed to main.py).
        # Don't use all words in self.cls_bg_delta_strings in the same prompt. Otherwise after taking the average,
        # the resulting embedding may have weird semantics and match too many areas.
        cls_bg_delta_string = self.cls_bg_delta_strings[subject_idx]
        
        example["subj_string"] = subject_string

        # If num_vectors_per_token == 3:
        # "z"    => "z, , "
        # "girl" => "girl, , "
        # Need to leave a space between multiple ",,", otherwise they are treated as one token.
        if self.num_vectors_per_token > 1:
            subject_string          += ", " * (self.num_vectors_per_token - 1)
            cls_delta_string   += ", " * (self.num_vectors_per_token - 1)
        if self.num_vectors_per_bg_token > 1 and background_string is not None:
            background_string       += ", " * (self.num_vectors_per_bg_token - 1)
            cls_bg_delta_string     += ", " * (self.num_vectors_per_bg_token - 1)

        if self.common_placeholder_prefixes is not None:
            common_placeholder_prefix = random.choice(self.common_placeholder_prefixes)
            subject_string          = common_placeholder_prefix + " " + subject_string
            cls_delta_string   = common_placeholder_prefix + " " + cls_delta_string
        # common_placeholder_prefixes are specified for red_cartoon.
        # compos_placeholder_prefixes are specified for fixhand.
        # They usually won't be used together. 
        # If both common_placeholder_prefixes and compos_placeholder_prefixes happen to be specified,
        # then the prompt is like "a photo of a compos_placeholder_prefix common_placeholder_prefix z ...".
        if self.compos_placeholder_prefixes is not None:
            compos_placeholder_prefix = random.choice(self.compos_placeholder_prefixes)
            compos_subject_string   = compos_placeholder_prefix + " " + subject_string
            compos_cls_delta_string = compos_placeholder_prefix + " " + cls_delta_string
        else:
            compos_subject_string   = subject_string
            compos_cls_delta_string = cls_delta_string

        template = random.choice(imagenet_templates_small)

        subj_prompt_single  = template
        cls_prompt_single   = template

        bg_suffix     = " with background {}".format(background_string)   if background_string   is not None else ""
        # If background_string is None, then cls_bg_delta_string is None as well, thus cls_bg_suffix is "".
        cls_bg_suffix = " with background {}".format(cls_bg_delta_string) if cls_bg_delta_string is not None else ""
        # bug_suffix: " with background y". cls_bg_suffix: " with background grass/rock".
        subject_string_with_bg          = subject_string        + bg_suffix
        compos_subject_string_with_bg   = compos_subject_string + bg_suffix
        compos_cls_delta_string_with_bg = compos_cls_delta_string   + cls_bg_suffix

        # "face portrait" trick for humans/animals.
        if broad_class == 1:
            fp_prompt_template = "a face portrait of a {}"
            subj_prompt_single_fp = fp_prompt_template
            cls_prompt_single_fp  = fp_prompt_template
            subj_prompt_comps_fp  = []
            cls_prompt_comps_fp   = []

        subj_prompt_comps = []
        cls_prompt_comps  = []

        if is_animal:
            subj_type = "animal" 
        else:
            subj_type = "object"

        for _ in range(self.num_compositions_per_image):
            compositions_partial = sample_compositions(1, subj_type, is_training=True)
            composition_partial = compositions_partial[0]
            subj_prompt_comp    = subj_prompt_single + " " + composition_partial
            cls_prompt_comp     = cls_prompt_single  + " " + composition_partial
            subj_prompt_comps.append(subj_prompt_comp)
            cls_prompt_comps.append(cls_prompt_comp)

            if broad_class == 1:
                subj_prompt_comp_fp = subj_prompt_single_fp + " " + composition_partial
                cls_prompt_comp_fp  = cls_prompt_single_fp  + " " + composition_partial
                subj_prompt_comps_fp.append(subj_prompt_comp_fp)
                cls_prompt_comps_fp.append(cls_prompt_comp_fp)

        # NOTE: "caption" and "caption_bg" are only for image reconstruction iterations.
        # But subj_prompt_single must align with cls_prompt_single, subj_prompt_comp, cls_prompt_comp.
        # So they are different when compos_placeholder_prefix is specified.
        # Only "caption" and "caption_bg" are formatted with subject_string and subject_string_with_bg.
        # Other 4 types of prompts are formatted with compos_subject_string and compos_cls_delta_string.
        example["caption"]              = subj_prompt_single.format(subject_string)
        example["caption_bg"]           = subj_prompt_single.format(subject_string_with_bg)

        example["subj_prompt_single"]   = subj_prompt_single.format(compos_subject_string)
        example["cls_prompt_single"]    = cls_prompt_single.format(compos_cls_delta_string)
        # Will be split by "|" in the ddpm trainer.
        subj_prompt_comp = "|".join([ subj_prompt_comp.format(compos_subject_string) for subj_prompt_comp in subj_prompt_comps])
        cls_prompt_comp  = "|".join([ cls_prompt_comp.format(compos_cls_delta_string)     for cls_prompt_comp  in cls_prompt_comps])
        example["subj_prompt_comp"]     = subj_prompt_comp
        example["cls_prompt_comp"]      = cls_prompt_comp

        if bg_suffix:
            example["subj_prompt_single_bg"] = subj_prompt_single.format(compos_subject_string_with_bg)
            example["cls_prompt_single_bg"]  = cls_prompt_single.format(compos_cls_delta_string_with_bg)
            # *_comp_bg prompts are for static delta loss on training images.
            example["subj_prompt_comp_bg"]   = "|".join([ subj_prompt_comp.format(compos_subject_string_with_bg) for subj_prompt_comp in subj_prompt_comps])
            example["cls_prompt_comp_bg"]    = "|".join([ cls_prompt_comp.format(compos_cls_delta_string_with_bg)     for cls_prompt_comp  in cls_prompt_comps])

        if broad_class == 1:
            # Delta loss requires subj_prompt_single/cls_prompt_single to be token-wise aligned
            # with subj_prompt_comp/cls_prompt_comp, so we need to specify them in the dataloader as well.
            example["subj_prompt_single_fp"] = subj_prompt_single_fp.format(compos_subject_string)
            example["cls_prompt_single_fp"]  = cls_prompt_single_fp.format(compos_cls_delta_string)
            example["subj_prompt_comp_fp"]   = "|".join([ subj_prompt_comp_fp.format(compos_subject_string) for subj_prompt_comp_fp in subj_prompt_comps_fp]) 
            example["cls_prompt_comp_fp"]    = "|".join([ cls_prompt_comp_fp.format(compos_cls_delta_string)     for cls_prompt_comp_fp  in cls_prompt_comps_fp])

            if bg_suffix:
                example["subj_prompt_single_fp_bg"] = subj_prompt_single_fp.format(compos_subject_string_with_bg)
                example["cls_prompt_single_fp_bg"]  = cls_prompt_single_fp.format(compos_cls_delta_string_with_bg)
                # *_comp_bg prompts are for static delta loss on training images.
                example["subj_prompt_comp_fp_bg"]   = "|".join([ subj_prompt_comp_fp.format(compos_subject_string_with_bg) for subj_prompt_comp_fp in subj_prompt_comps_fp])
                example["cls_prompt_comp_fp_bg"]    = "|".join([ cls_prompt_comp_fp.format(compos_cls_delta_string_with_bg)     for cls_prompt_comp_fp  in cls_prompt_comps_fp])

    def repl_bg_as_wbg(self, prompt, subject_idx):
        background_string = self.background_strings[subject_idx]
        wds_background_string = self.wds_background_strings[subject_idx]
        if wds_background_string is None:
            return prompt
        # Replace singleton 'y' with 'w'.
        prompt2 = re.sub(rf"(?<=(\W|^)){background_string}(?=(\W|$))", 
                         wds_background_string, prompt)
        return prompt2
    
# Randomly sample a subject number.
# This subject number will be used by an PersonalizedBase instance to draw random images.
# epoch_len: number of batches in one epoch. Usually initialized to be the same 
# as the number of batches of the training data.
class SubjectSampler(Sampler):
    def __init__(self, num_subjects, num_batches, batch_size, debug=False):
        self.batch_size = batch_size
        # num_batches: +1 to make sure the last batch is also used.
        self.num_batches  = num_batches + 1
        self.num_subjects = num_subjects
        assert self.num_subjects > 0, "FATAL: no subjects found in the dataset!"
        print("SubjectSampler initialized on {} subjects, batches: {}*{}".format(self.num_subjects, 
                                                                                 self.batch_size, self.num_batches))

        self.switch_cycle_length = self.batch_size
        self.curr_subj_idx = 0
        self.curr_subj_count = 0

    def __len__(self):
        return self.num_batches * self.batch_size
    
    def next_subject(self):
        new_subj_idx = random.randint(0, self.num_subjects - 1)
        return new_subj_idx

    def __iter__(self):
        # Output will be like:
        # 0, 0, ..., 0 (repeat batch_size times), 1, 1, ..., 1 (repeat batch_size times) ...
        # So that samples in each batch have the same chapter number.
        for i in range(self.num_batches * self.batch_size):
            # If the current subject index has been repeated batch_size times, 
            # we find the next subject index.
            if self.curr_subj_count >= self.switch_cycle_length:
                self.curr_subj_idx   = self.next_subject()
                self.curr_subj_count = 0        

            self.curr_subj_count += 1
            yield self.curr_subj_idx, True
