"""Setup file for dannce."""
from setuptools import setup, find_packages

setup(
    name="sdannce",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "six",
        "pyyaml",
        "imageio==2.8.0",
        "imageio-ffmpeg",
        "numpy",
        "scikit-image",
        "matplotlib",
        "attr",
        "attrs",
        "multiprocess",
        "opencv-python",
        'tensorboard',
        "mat73",
        "psutil",
        "tqdm",
        "pandas"
    ],
    # scripts=[],
    entry_points={
        "console_scripts": [
            "dannce-predict-sbatch = dannce.cli:sbatch_dannce_predict_cli",
            "dannce-train-sbatch = dannce.cli:sbatch_dannce_train_cli",
            "com-predict-sbatch = dannce.cli:sbatch_com_predict_cli",
            "com-train-sbatch = dannce.cli:sbatch_com_train_cli",
            "dannce-train = dannce.cli:dannce_train_cli",
            "dannce-train-grid = cluster.grid:dannce_train_grid",
            "dannce-predict = dannce.cli:dannce_predict_cli",
            "sdannce-train = dannce.cli:sdannce_train_cli",
            "sdannce-predict = dannce.cli:sdannce_predict_cli",
            "com-train = dannce.cli:com_train_cli",
            "com-predict = dannce.cli:com_predict_cli",
            "dannce-predict-multi-gpu = cluster.multi_gpu:dannce_predict_multi_gpu",
            "com-predict-multi-gpu = cluster.multi_gpu:com_predict_multi_gpu",
            "dannce-predict-single-batch = cluster.multi_gpu:dannce_predict_single_batch",
            "dannce-train-single-batch = cluster.grid:dannce_train_single_batch",
            "com-predict-single-batch = cluster.multi_gpu:com_predict_single_batch",
            "dannce-merge = cluster.multi_gpu:dannce_merge",
            "com-merge = cluster.multi_gpu:com_merge",
            "dannce-inference-sbatch = cluster.multi_gpu:submit_inference",
            "dannce-inference = cluster.multi_gpu:inference",
            "dannce-multi-instance-inference = cluster.multi_gpu:multi_instance_inference",
        ]
    },
)