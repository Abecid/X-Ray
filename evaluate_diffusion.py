import glob
from diffusers import UNetSpatioTemporalConditionModel
from src.xray_pipeline import StableVideoDiffusionPipeline
from diffusers.utils import load_image
import torch
from PIL import Image
import os
import numpy as np
import trimesh
import torchvision
import open3d as o3d
import torch.nn.functional as F
import shutil
from tqdm import tqdm
from src.chamfer_distance import compute_trimesh_chamfer
from scipy.sparse import csr_matrix
import argparse



def get_rays(directions, c2w):
    # Rotate ray directions from camera coordinate to the world coordinate
    rays_d = directions @ c2w[:3, :3].T # (H, W, 3)
    rays_d = rays_d / (np.linalg.norm(rays_d, axis=-1, keepdims=True) + 1e-8)
    
    # The origin of all rays is the camera origin in world coordinate
    rays_o = np.broadcast_to(c2w[:3, 3], rays_d.shape) # (H, W, 3)

    return rays_o, rays_d

def depth_to_pcd_normals(GenDepths, GenNormals, GenColors):
    camera_angle_x = 0.8575560450553894
    image_width = GenDepths.shape[-1]
    image_height = GenDepths.shape[-2]
    fx = 0.5 * image_width / np.tan(0.5 * camera_angle_x)

    rays_screen_coords = np.mgrid[0:image_height, 0:image_width].reshape(
            2, image_height * image_width).T  # [h, w, 2]

    grid = rays_screen_coords.reshape(image_height, image_width, 2)

    cx = image_width / 2.0
    cy = image_height / 2.0

    i, j = grid[..., 1], grid[..., 0]

    directions = np.stack([(i-cx)/fx, -(j-cy)/fx, -np.ones_like(i)], -1) # (H, W, 3)

    c2w = np.eye(4).astype(np.float32)

    rays_origins, ray_directions = get_rays(directions, c2w)
    rays_origins = rays_origins[None].repeat(GenDepths.shape[0], 0)
    ray_directions = ray_directions[None].repeat(GenDepths.shape[0], 0)

    GenDepths = GenDepths.transpose(0, 2, 3, 1)
    GenNormals = GenNormals.transpose(0, 2, 3, 1)
    GenColors = GenColors.transpose(0, 2, 3, 1)
    
    valid_index = GenDepths[..., 0] > 0
    rays_origins = rays_origins[valid_index]
    ray_directions = ray_directions[valid_index]
    GenDepths = GenDepths[valid_index]
    normals = GenNormals[valid_index]
    colors = GenColors[valid_index]
    xyz = rays_origins + ray_directions * GenDepths

    return xyz, normals, colors


def load_depths(depths_path):
    loaded_data = np.load(depths_path)
    loaded_sparse_matrix = csr_matrix((loaded_data['data'], loaded_data['indices'], loaded_data['indptr']), shape=loaded_data['shape'])
    original_shape = (16, 1+3+3, 256, 256)
    restored_array = loaded_sparse_matrix.toarray().reshape(original_shape)
    return restored_array

if __name__ == "__main__":

    parser = argparse.ArgumentParser("SVD Depth Inference")
    parser.add_argument("--exp", type=str, default="ShapeNet_Car", help="experiment name")
    parser.add_argument("--model_id", type=str, default="stabilityai/stable-video-diffusion-img2vid")
    parser.add_argument("--data_root", type=str, default="Data/ShapeNet_Car/depths", help="data root")
    args = parser.parse_args()

    near = 0.6
    far = 2.4

    exp_name = args.exp
    model_id = args.model_id
    xray_root = args.data_root

    pipe = StableVideoDiffusionPipeline.from_pretrained(model_id, 
                                torch_dtype=torch.float16, variant="fp16").to("cuda")

    # Get the most recent checkpoint
    dirs = os.listdir(os.path.join("Output", exp_name))
    dirs = [d for d in dirs if d.startswith("checkpoint")]
    dirs = sorted(dirs, key=lambda x: int(x.split("-")[1]))
    ckpt_name = dirs[-1]
    print("restore from", f"Output/{exp_name}/{ckpt_name}/unet")

    pipe.unet = UNetSpatioTemporalConditionModel.from_pretrained(
            f"Output/{exp_name}/{ckpt_name}",
            subfolder="unet",
            torch_dtype=torch.float16,
        ).to("cuda")

    height = 512
    width = 512
    prompt = ""

    xray_paths = glob.glob(os.path.join(xray_root, "**/*.npz"), recursive=True)
    image_paths = [x.replace("depths", "images").replace(".npz", ".png") for x in xray_paths]
    sorted(image_paths)
    image_paths = image_paths[::10] # test set
    image_paths = image_paths[::50]

    if os.path.exists(f"Output/{exp_name}/evaluate"):
        shutil.rmtree(f"Output/{exp_name}/evaluate")
    os.makedirs(f"Output/{exp_name}/evaluate", exist_ok=True)

    all_chamfer_distance = []
    progress_bar =  tqdm(range(len(image_paths)))
    for i in progress_bar:
        image_path = image_paths[i]
        uid = image_path.split("/")[-2]

        with torch.no_grad():
            image = load_image(image_path).resize((width, height), Image.BILINEAR).convert("RGB")
            outputs = pipe(image,
                            height=height,
                            width=width,
                            num_frames=8,
                            decode_chunk_size=8,
                            motion_bucket_id=127,
                            fps=7,
                            noise_aug_strength=0.0,
                            output_type="latent").frames[0]
            outputs = outputs.clamp(-1, 1) # clamp to [-1, 1]

        image.save(f"Output/{exp_name}/evaluate/{uid}_original.png")
        GenDepths = (outputs[:, 0:1].cpu().numpy() * 0.5 + 0.5) * (far - near) + near
        GenDepths[GenDepths <= near] = 0
        GenDepths[GenDepths >= far] = 0
        GenDepths_ori = GenDepths.copy()
        for i in range(GenDepths.shape[0]-1):
            GenDepths[i+1] = np.where(GenDepths_ori[i+1] < GenDepths_ori[i], 0, GenDepths_ori[i+1])

        GenNormals = F.normalize(outputs[:, 1:4], dim=1).cpu().numpy()
        GenColors = (outputs[:, 4:7].cpu().numpy() * 0.5 + 0.5)

        gen_pts, gen_normals, gen_colors = depth_to_pcd_normals(GenDepths, GenNormals, GenColors)
        gen_pts[:, 2] += 1.5
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(gen_pts)
        pcd.normals = o3d.utility.Vector3dVector(gen_normals)
        pcd.colors = o3d.utility.Vector3dVector(gen_colors[..., :3])
        o3d.io.write_point_cloud(f"Output/{exp_name}/evaluate/{uid}_prd.ply", pcd)
        
        gt_path = image_path.replace("images", "depths").replace(".png", ".npz")
        xray = load_depths(gt_path)[:8]
        GtDepths = xray[:, 0:1]
        GtNormals = xray[:, 1:4]
        GtColors = xray[:, 4:7]
        gt_pts, gt_normals, gt_colors = depth_to_pcd_normals(GtDepths, GtNormals, GtColors)
        gt_pts[:, 2] += 1.5
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(gt_pts)
        pcd.normals = o3d.utility.Vector3dVector(gt_normals)
        pcd.colors = o3d.utility.Vector3dVector(gt_colors)
        o3d.io.write_point_cloud(f"Output/{exp_name}/evaluate/{uid}_gt.ply", pcd)

        chamfer_distance = compute_trimesh_chamfer(gt_pts, gen_pts)
        # if not nan
        if chamfer_distance == chamfer_distance:
            all_chamfer_distance += [chamfer_distance]
            progress_bar.set_postfix({"chamfer_distance": np.mean(all_chamfer_distance)})
            progress_bar.update(1)
        