"""make variations of input image"""
import argparse, os, sys, glob
import PIL
import torch
import numpy as np
from omegaconf import OmegaConf
from PIL import Image
from tqdm import tqdm, trange
from itertools import islice
from einops import rearrange, repeat
from torchvision.utils import make_grid
from torch import autocast
from contextlib import nullcontext
from pytorch_lightning import seed_everything
from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
import transformers
# from ldm.models.diffusion.plms, import PLMSSampler
import torchvision.transforms as transforms
import matplotlib.pyplot as plt
import torchvision.models as models
from PIL import ImageOps
import cv2
import numpy as np
import torch.nn.functional as F
transformers.logging.set_verbosity_error()

def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())

def load_model_from_config(config, ckpt,cuda, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)
    if cuda and not torch.cuda.is_available():
        print("ERROR: cuda is not available, try running on CPU")
        sys.exit(1)
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    model = model.to(device)
    model.eval()
    return model,device

def load_img(path):
    image = Image.open(path).convert("L")  
    w, h = image.size
    print(f"loaded input image of size ({w}, {h}) from {path}")
    
    if w > h:
        new_w, new_h = 512, int(512 * (h / w))
    else:
        new_w, new_h = int(512 * (w / h)), 512
    
    image = image.resize((new_w, new_h), resample=Image.LANCZOS)
    
    left = (new_w - 512) // 2
    top = (new_h - 512) // 2
    right = left + 512
    bottom = top + 512
    
    image = image.crop((left, top, right, bottom))
    
    image = np.array(image).astype(np.float32) / 255.0
    image = np.repeat(image[:, :, np.newaxis], 3, axis=2)  
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return 2. * image - 1.

def split_human_and_background(image):
    # FCN(Fully Convolutional Network) 모델 로드 및 평가 모드 설정
    net = models.segmentation.fcn_resnet101(pretrained=True)
    net.eval()  

    try:
        # 이미지 전처리 및 사람 분리
        
        # 이미지를 텐서로 변환하고, 정규화를 수행합니다.
        # Image를 텐서로 변환하는 단계입니다.
        preprocess = transforms.Compose([
            transforms.ToTensor(),  # PIL Image를 텐서로 변환합니다.
            # ImageNet 데이터셋의 평균(mean) 및 표준편차(std)를 사용하여 정규화합니다.
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])
        input_tensor = preprocess(image)  # 입력 이미지를 전처리합니다.
        input_batch = input_tensor.unsqueeze(0)  # 배치 차원을 추가합니다.

        with torch.no_grad():
            # 이미지를 FCN 모델에 입력으로 주고 예측(prediction)을 수행합니다.
            prediction = net(input_batch)['out']

        # 모델의 출력에서 가장 높은 값을 갖는 클래스로 예측합니다.
        output_predictions = prediction.squeeze().argmax(dim=0).cpu().numpy()

        # 사람 영역을 마스킹합니다. (클래스 15는 일반적으로 사람을 의미합니다.)
        human_mask = output_predictions == 15

        # 사람 영역만 남기고 배경을 0으로 만듭니다.
        human_only = np.array(image)
        human_only[~human_mask] = 0

        # 배경 영역만 남기고 사람을 0으로 만듭니다.
        background = np.array(image)
        background[human_mask] = 0

        # 사람 영역만 있는 이미지와 배경만 있는 이미지를 반환합니다.
        return Image.fromarray(human_only), Image.fromarray(background)

    except Exception as e:
        # 예외가 발생한 경우 에러를 출력하고 None을 반환합니다.
        print(f"Error occurred: {e}")
        return None, None  # Error occurred, return None for both images

# # 사람이 빈 배경화면을 자연스럽게 채워주는 함수
# def inpaint_background(human_image, background_image_np):
#     # 사람 이미지를 NumPy 배열로 변환하고, 그레이스케일로 변환
#     human_mask_np = np.array(human_image)
#     human_mask_gray = cv2.cvtColor(human_mask_np, cv2.COLOR_RGB2GRAY)
#     # 이진화를 통해 마스크 생성 (사람이 있는 부분을 표시)
#     _, binary_mask = cv2.threshold(human_mask_gray, 1, 255, cv2.THRESH_BINARY)
#     # OpenCV의 인페인팅 함수 사용
#     inpainted_background = cv2.inpaint(background_image_np, binary_mask, 3, cv2.INPAINT_TELEA)
#     # 마스크 영역을 반환
#     return inpainted_background, binary_mask

#배경이미지와 사람이미지를 다시 합치는 함수
def merge_human_and_background(human_image, background_image):
    # 배경 이미지를 RGB 모드로 변환 및 크기 조정
    background_image_rgb = background_image.convert("RGB").resize(human_image.size)
    # 사람 이미지를 그레이스케일로 변환하고, 투명도 마스크로 사용할 수 있도록 255로 스케일링
    mask = ImageOps.grayscale(human_image)
    mask = mask.point(lambda x: 255 if x > 0 else 0)
    # 여기서 마스크를 확인할 수 있도록 저장해봅니다.
    mask.save("path_to_save_your_mask.png")
    # 사람 이미지를 배경 이미지 위에 합성
    final_composite = Image.composite(human_image.convert("RGB"), background_image_rgb, mask)
    return final_composite
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompt",
        type=str,
        nargs="?",
        default="ink wash landscape painting",
        help="the prompt to render"
    )
    parser.add_argument(
        "--init-img",
        type=str,
        nargs="?",
        help="path to the input image"
    )
    parser.add_argument(
        "--outdir",
        type=str,
        nargs="?",
        help="dir to write results to",
        default="/outputs/img2img"
    )
    parser.add_argument(
        "--skip_grid",
        action='store_true',
        help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
    )
    parser.add_argument(
        "--cuda",
        type=int,
        default=1,
        help="set it to 1 for running on GPU, 0 for CPU",
    )
    
    parser.add_argument(
        "--skip_save",                    #이 옵션이 사용되면 개별 샘플을 저장하지 않습니다. 주로 속도 측정에 사용됩니다. 이 옵션을 사용하면 샘플링 결과를 개별 파일로 저장하지 않고, 측정이나 실험을 위한 프로세스의 속도를 향상시킬 수 있습니다.
        action='store_true',
        help="do not save indiviual samples. For speed measurements.",
    )
    parser.add_argument(
        "--ddim_steps",     # DDim 샘플링 단계 수를 나타냅니다. 이 옵션은 DDim 샘플링 프로세스에서 수행되는 단계의 총 수를 결정합니다. 이 값이 클수록 더 많은 샘플링 단계를 수행하게 되며, 결과적으로 더 많은 계산이 필요합니다. 이 값을 조절하여 샘플링 프로세스의 정교성을 조절할 수 있습니다.
        type=int,
        default=250,
        help="number of ddim sampling steps",
    )
    parser.add_argument(
        "--fixed_code",
        action='store_true',
        help="if enabled, uses the same starting code across all samples ",
    )
    # parser.add_argument(
    #     "--plms",
    #     action='store_true',
    #     help="use plms sampling(PLMS sampler not yet supported)",
    # )
    parser.add_argument(
        "--ddim_eta",      #0.0으로 설정하면, 생성된 이미지는 완전히 예측 가능한 방식으로 만들어집니다. 하지만 eta 값을 늘리면 조금 더 예상할 수 없는 이미지가 생성
        type=float,        # 이를 통해 생성되는 이미지의 변화와 다양성을 조절할 수 있음
        default=0.0,       # eta를 늘릴수록 이미지가 더 랜덤하고 다양해짐
        help="ddim eta (eta=0.0 corresponds to deterministic sampling",
    )
    parser.add_argument(
        "--n_iter",
        type=int,     #샘플링을 얼마나 자주 실행할지 결정하는 값
        default=1,
        help="sample this often",
    )
    parser.add_argument(
        "--C",
        type=int,            # 잠재 공간(latent space)의 채널 수.
        default=4,
        help="latent channels",
    )
    parser.add_argument(
        "--f",
        type=int,           #  다운샘플링 인자(factor)로, 주로 8 또는 16
        default=8,
        help="downsampling factor, most often 8 or 16",
    )
    parser.add_argument(
        "--n_samples",
        type=int,
        default=1,         #각 프롬프트(prompt)에 대해 생성할 샘플의 수. 배치 크기를 나타냅니다.
        help="how many samples to produce for each given prompt. A.k.a batch size",
    )
    parser.add_argument(
        "--n_rows",
        type=int,          #그리드(grid)에 있는 행의 수입니다. 기본적으로는 n_samples와 동일합니다.
        default=0,
        help="rows in the grid (default: n_samples)",
    )
    parser.add_argument(
        "--scale",
        type=float,          # 무조건적인 가이드 스케일(unconditional guidance scale). 빈 이미지와 조건을 가진 이미지 간의 차이를 나타냅니다.
        default=10.0,         # 이미지 생성 과정에서 사용되는 노이즈 추가 및 노이즈 제거의 강도를 조절하는 데 사용
        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
    )
    parser.add_argument(
        "--strength",
        type=float,
        default=0.2,
        help="strength for noising/unnoising. 1.0 corresponds to full destruction of information in init image",
    )
    parser.add_argument(
        "--from-file",
        type=str,                         # 지정된 파일에서 프롬프트를 로드하는 옵션입니다.
        help="if specified, load prompts from this file",
    )
    parser.add_argument(
        "--config",                           # 모델을 구성하는 설정 파일의 경로
        type=str,
        default="/configs/stable-diffusion/v1-inference.yaml",
        help="path to config which constructs model",
    )
    parser.add_argument(
        "--ckpt",
        type=str,                          # 모델 체크포인트 파일의 경로
        default="/models/ldm/stable-diffusion/soomuk_all.ckpt",
        help="path to checkpoint of model",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="the seed (for reproducible sampling)",
    )
    parser.add_argument(
        "--precision",
        type=str,                            # 평가를 어떤 정밀도(precision)로 수행할지 선택하는 옵션으로, full 또는 autocast 중 하나를 선택할 수 있음
        help="evaluate at this precision",   # Full은 fp32 autocast은 fp16
        choices=["full", "autocast"],        # 일반적으로 full은 정확도를 중시하는 작업이나 모델 평가 시에 사용되며,                                        
        default="autocast"                   #autocast는 속도와 메모리 효율을 높이는 데에 활용   
    )
    opt = parser.parse_args()
    seed_everything(opt.seed)
    config = OmegaConf.load(f"{opt.config}")
    model,device = load_model_from_config(config, f"{opt.ckpt}",opt.cuda)
    
    # if opt.plms:
    #     raise NotImplementedError("PLMS sampler not (yet) supported")
    #     sampler = PLMSSampler(model)
    # else:
    sampler = DDIMSampler(model,device=device)
    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir
    batch_size = opt.n_samples
    n_rows = opt.n_rows if opt.n_rows > 0 else batch_size
    if not opt.from_file:
        prompt = opt.prompt 
        assert prompt is not None
        data = [batch_size * [prompt]]
    else:
        print(f"reading prompts from {opt.from_file}")
        with open(opt.from_file, "r") as f:
            data = f.read().splitlines()
            data = list(chunk(data, batch_size))
    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)
    base_count = len(os.listdir(sample_path))
    grid_count = len(os.listdir(outpath)) - 1
    assert os.path.isfile(opt.init_img)  #input image
    init_image = load_img(opt.init_img).to(device)
    # 사람과 배경 분리
    human_image, background_image = split_human_and_background(init_image)
    # 원본 이미지를 텐서로 변환
    original_image_tensor = transforms.ToTensor()(Image.open(opt.init_img).convert('RGB')).unsqueeze(0).to(device)
    # 입력 이미지 크기 조정 (예: 512x512로 조정)
    original_image_tensor = F.interpolate(original_image_tensor, size=(512, 512), mode='bilinear')
    # 초기 이미지에 대한 처리
    init_latent = model.get_first_stage_encoding(model.encode_first_stage(original_image_tensor))
    #여기서 부터 모델 적용
    #모델의 첫 번째 단계에 해당하는 인코딩 과정으로, 입력 이미지를 모델이 이해할 수 있는 형태로 변환하여 latent space로 이동
    #인코딩된 이미지를 잠재 공간(latent space)으로 변환하여 활용할 수 있도록 준비하는 역할
    sampler.make_schedule(ddim_num_steps=opt.ddim_steps, ddim_eta=opt.ddim_eta, verbose=False)
    #Discrete Diffusion Model (DDIM)의 스케줄을 만드는 역할. 
    #DDIM에 필요한 다양한 파라미터 및 계산을 수행. 주로 DDIM 샘플링에 사용되는 시간 단계, 알파 값 등을 계산하고 버퍼로 등록
    assert 0. <= opt.strength <= 1., 'can only work with strength in [0.0, 1.0]'  #강도, 1에 가까울 수록 스타일 이미지가 강해짐
    t_enc = int(opt.strength * opt.ddim_steps) #opt.strength와 opt.ddim_steps를 곱하여 초기 이미지를 얼마나 변화시킬지를 결정하는 값
    print(f"target t_enc is {t_enc} steps")
    # 정밀도 설정
    precision_scope = autocast if opt.precision == "autocast" else nullcontext  # 정밀도 설정
    with torch.no_grad():  # 그래디언트를 계산하지 않는 상태로 torch 연산 수행
        with precision_scope("cuda"):  # GPU 상에서의 연산 정밀도를 설정
            with model.ema_scope():  # Exponential Moving Average(EMA) 적용
                all_samples = list()  # 생성된 이미지 샘플들을 담을 리스트 초기화
                for n in trange(opt.n_iter, desc="Sampling"):  # 주어진 횟수만큼 이미지 생성 반복
                    for prompts in tqdm(data, desc="data"):  # 데이터에 대한 루프
                        uc = None  # 초기화된 조건 설정
                        if opt.scale != 1.0:  # 스케일이 1이 아닌 경우
                            uc = model.get_learned_conditioning(batch_size * [""])  # 학습된 조건 가져오기
                        if isinstance(prompts, tuple):  # prompts가 튜플 형식인 경우
                            prompts = list(prompts)  # 리스트로 변환
                        c = model.get_learned_conditioning(prompts)  # 조건 설정
                        # 인코딩 (스케일 조정된 잠재 변수)
                        z_enc = sampler.stochastic_encode(init_latent, torch.tensor([t_enc] * batch_size).to(device))
                        # 디코딩
                        samples = sampler.decode(z_enc, c, t_enc, unconditional_guidance_scale=opt.scale,
                                                unconditional_conditioning=uc)
                        x_samples = model.decode_first_stage(samples)  # 디코딩된 이미지 생성
                        x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)  # 이미지 클램핑
                        if not opt.skip_save:  # 이미지 저장 옵션이 비활성화되지 않은 경우
                            for x_sample in x_samples:  # 모든 이미지에 대해
                                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')  # 이미지 재구성 # c:채널, h:높이, w:너비
                                Image.fromarray(x_sample.astype(np.uint8)).save(  # 이미지를 파일로 저장
                                    os.path.join(sample_path, f"{base_count:05}.png"))  # 파일 경로 설정
                                base_count += 1  # 저장된 이미지 수 증가
                        all_samples.append(x_samples)  # 생성된 이미지 샘플을 리스트에 추가
                if not opt.skip_grid:  # 그리드 저장 옵션이 비활성화되지 않은 경우
                    # 그리드로 이미지 샘플 저장
                    grid = torch.stack(all_samples, 0)
                    grid = rearrange(grid, 'n b c h w -> (n b) c h w')  # 그리드 구성   # n: 샘플개수,b: 배치 크기,c:채널, h:높이, w:너비
                    grid = make_grid(grid, nrow=n_rows)  # 이미지 그리드 생성
                    # 이미지로 저장
                    grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()  # 이미지 조정
                    Image.fromarray(grid.astype(np.uint8)).save(os.path.join(outpath, f'grid-{grid_count:04}.png'))  # 이미지 저장
                    grid_count += 1  # 저장된 이미지 수 증가
    # 사람 이미지와 합성
    # 첫 번째 샘플을 선택 (index 0)
    selected_sample = x_samples[0]
    # 텐서를 PIL 이미지로 변환
    selected_sample = 255. * rearrange(selected_sample.cpu().numpy(), 'c h w -> h w c')
    selected_sample_image = Image.fromarray(selected_sample.astype(np.uint8))
    # 변형된 배경 이미지와 사람 이미지를 합성
    final_image = merge_human_and_background(human_image, selected_sample_image)
    
    # 최종 이미지를 그레이스케일로 변환
    final_image_gray = final_image.convert("L")
    # 그레이스케일 이미지 저장
    final_image_gray.save(os.path.join(outpath, "final_composite_gray.png"))
    print(f"Your grayscale image is ready and waiting for you here: \n{outpath} \n \nEnjoy.")
if __name__ == "__main__":
    main()