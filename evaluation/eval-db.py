import re
import argparse
import glob
import os

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--guidance_scale", type=float, default=10, help="guidance scale")
    parser.add_argument("--gpu", type=int, default=0, help="gpu id")
    parser.add_argument("--method", default='ada', choices=["ada", "ti", "db"], type=str, 
                        help="method to use for generating samples")
    parser.add_argument("--placeholder", type=str, default="z", 
                        help="placeholder token for the subject")
    parser.add_argument("--no_append_class", dest='append_class', action="store_false",
                        help="Do not append class token to the subject placeholder (default: append)")
    parser.add_argument("--scale", type=float, default=5, 
                        help="the guidance scale")
    parser.add_argument("--n_samples", type=int, default=4, 
                        help="number of samples to generate for each test case")
    parser.add_argument("--bs", type=int, default=4, 
                        help="batch size")
    
    parser.add_argument("--steps", type=int, default=50, 
                        help="number of DDIM steps to generate samples")
    parser.add_argument("--ckpt_dir", type=str, default="logs",
                        help="parent directory containing checkpoints of all subjects")
    parser.add_argument("--ckpt_iter", type=int, default=4000,
                        help="checkpoint iteration to use")
    parser.add_argument("--out_dir_tmpl", type=str, default="samples-dbeval",
                        help="Template of parent directory to save generated samples")

    # composition case file path
    parser.add_argument("--subject_file", type=str, default="scripts/info-db-eval-subjects.sh", 
                        help="subject info script file")
    # range of subjects to generate
    parser.add_argument("--range", type=str, default=None, 
                        help="Range of subjects to generate (Index starts from 1 and is inclusive, e.g., 1-30)")
    args = parser.parse_args()
    return args

def split_string(input_string):
    pattern = r'"[^"]*"|\S+'
    substrings = re.findall(pattern, input_string)
    substrings = [ s.strip('"') for s in substrings ]
    return substrings

def parse_subject_file(subject_file_path):
    subjects, class_tokens = None, None

    with open(subject_file_path, "r") as f:
        lines = f.readlines()
        lines = [line.strip() for line in lines]
        for line in lines:
            if re.search(r"^set -[la] (subjects|db_prompts|ada_prompts)", line):
                # set -l subjects  alexachung    alita...
                mat = re.search(r"^set -[la] (subjects|db_prompts|ada_prompts)\s+(\S.+\S)", line)
                if mat is not None:
                    var_name = mat.group(1)
                    substrings = split_string(mat.group(2))
                    if var_name == "subjects":
                        subjects = substrings
                    elif var_name == "db_prompts":
                        class_tokens = substrings
                else:
                    breakpoint()

    if subjects is None or class_tokens is None:
        raise ValueError("subjects or db_prompts is None")
    
    return subjects, class_tokens

def get_promt_list(subject_name, unique_token, class_token):
    object_prompt_list = [
    # The space between "{0} {1}" is removed, so that prompts for ada/ti could be generated by
    # providing an empty class_token. To generate prompts for DreamBooth, 
    # provide a class_token starting with a space.
    'a {0}{1} in the jungle',
    'a {0}{1} in the snow',
    'a {0}{1} on the beach',
    'a {0}{1} on a cobblestone street',
    'a {0}{1} on top of pink fabric',
    'a {0}{1} on top of a wooden floor',
    'a {0}{1} with a city in the background',
    'a {0}{1} with a mountain in the background',
    'a {0}{1} with a blue house in the background',
    'a {0}{1} on top of a purple rug in a forest',
    'a {0}{1} with a wheat field in the background',
    'a {0}{1} with a tree and autumn leaves in the background',
    'a {0}{1} with the Eiffel Tower in the background',
    'a {0}{1} floating on top of water',
    'a {0}{1} floating in an ocean of milk',
    'a {0}{1} on top of green grass with sunflowers around it',
    'a {0}{1} on top of a mirror',
    'a {0}{1} on top of the sidewalk in a crowded street',
    'a {0}{1} on top of a dirt road',
    'a {0}{1} on top of a white rug',
    'a red {0}{1}',
    'a purple {0}{1}',
    'a shiny {0}{1}',
    'a wet {0}{1}',
    'a cube shaped {0}{1}'
    ]

    animal_prompt_list = [
    'a {0}{1} in the jungle',
    'a {0}{1} in the snow',
    'a {0}{1} on the beach',
    'a {0}{1} on a cobblestone street',
    'a {0}{1} on top of pink fabric',
    'a {0}{1} on top of a wooden floor',
    'a {0}{1} with a city in the background',
    'a {0}{1} with a mountain in the background',
    'a {0}{1} with a blue house in the background',
    'a {0}{1} on top of a purple rug in a forest',
    'a {0}{1} wearing a red hat',
    'a {0}{1} wearing a santa hat',
    'a {0}{1} wearing a rainbow scarf',
    'a {0}{1} wearing a black top hat and a monocle',
    'a {0}{1} in a chef outfit',
    'a {0}{1} in a firefighter outfit',
    'a {0}{1} in a police outfit',
    'a {0}{1} wearing pink glasses',
    'a {0}{1} wearing a yellow shirt',
    'a {0}{1} in a purple wizard outfit',
    'a red {0}{1}',
    'a purple {0}{1}',
    'a shiny {0}{1}',
    'a wet {0}{1}',
    'a cube shaped {0}{1}'
    ]

    if re.search("^(cat|dog)", subject_name):
        orig_prompt_list = animal_prompt_list
    else:
        orig_prompt_list = object_prompt_list
    
    prompt_list = [ prompt.format(unique_token, class_token) for prompt in orig_prompt_list ]
    return prompt_list, orig_prompt_list

def find_first_match(lst, search_term):
    for item in lst:
        if search_term in item:
            return item
    return None  # If no match is found

args = parse_args()
subjects, class_tokens = parse_subject_file(args.subject_file)
if args.method == 'db':
    args.append_class = True

if args.range is not None:
    range_strs = args.range.split("-")
    # low is 1-indexed, converted to 0-indexed by -1.
    # high is inclusive, converted to exclusive without adding offset.
    low, high  = int(range_strs[0]) - 1, int(range_strs[1])
    subjects   = subjects[low:high]
    class_tokens = class_tokens[low:high]

all_ckpts = os.listdir(args.ckpt_dir)
all_ckpts.sort(key=lambda x: os.path.getmtime(os.path.join(args.ckpt_dir, x)), reverse=True)

for subject_name, class_token in zip(subjects, class_tokens):
    ckpt_sig   = subject_name + "-" + args.method
    ckpt_name  = find_first_match(all_ckpts, ckpt_sig)
    if ckpt_name is None:
        print("ERROR: No checkpoint found for subject: " + subject_name)
        #continue
        # breakpoint()

    if args.append_class:
        # For DreamBooth, append_class is the default.
        # For Ada/TI, if we append class token to "z" -> "z dog", 
        # the chance of occasional under-expression of the subject may be reduced.
        # (This trick is not needed for human faces)
        # Prepend a space to class_token to avoid "a zcat" -> "a z cat"
        class_token = " " + class_token
    else:
        class_token = ""

    if args.method == 'db':
        config_file = "v1-inference.yaml"
        ckpt_path   = f"logs/{ckpt_name}/checkpoints/last.ckpt"
    else:
        config_file = "v1-inference-" + args.method + ".yaml"
        ckpt_path   = "models/stable-diffusion-v-1-4-original/sd-v1-4-full-ema.ckpt"
        emb_path    = f"logs/{ckpt_name}/checkpoints/embeddings_gs-{args.ckpt_iter}.pt"

    outdir = args.out_dir_tmpl + "-" + args.method
    prompt_list, orig_prompt_list = get_promt_list(subject_name, args.placeholder, class_token)
    prompt_filepath = f"{outdir}/{subject_name}-prompts.txt"
    os.makedirs(outdir, exist_ok=True)
    PROMPTS = open(prompt_filepath, "w")
    print(subject_name, ":")

    for prompt, orig_prompt in zip(prompt_list, orig_prompt_list):
        print("  ", prompt)
        indiv_subdir = subject_name + "-" + prompt.replace(" ", "-")
        # Repeat each prompt for n_samples times in the prompt file. 
        # So that stable_txt2img.py generates n_samples images for each prompt.
        for i in range(args.n_samples):
            # orig_prompt is saved in the prompt file as well, for evaluation later.
            PROMPTS.write( "\t".join([indiv_subdir, prompt, orig_prompt]) + "\n" )

    PROMPTS.close()
    # Since we use a prompt file, we don't need to specify --n_samples.
    command_line = f"python3 scripts/stable_txt2img.py --config configs/stable-diffusion/{config_file} --ckpt {ckpt_path} --ddim_eta 0.0 --ddim_steps {args.steps} --gpu {args.gpu} --from_file {prompt_filepath} --scale {args.scale} --n_repeat 1 --bs {args.bs} --outdir {outdir}"
    if args.method != 'db':
        command_line += f" --embedding_paths {emb_path}"

    print(command_line)
    os.system(command_line)
