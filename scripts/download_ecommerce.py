#!/usr/bin/env python3
"""下载 Kaggle Brazilian E-Commerce 数据集到本地（换机器一键重建原始数据）。

数据集：https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce

下载后无需任何再处理——CSV 是官方原样文件，import_ecommerce.py --csv-dir 直接消费
（列映射/类型转换在导入脚本里做，本脚本只负责把文件放到位的「下载即用」）。

两种下载方式（自动选择，可用 --method 强制）：
1. kaggle CLI：装了 kagglehub 或 kaggle 包且已配置凭证（~/.kaggle/kaggle.json 或
   KAGGLE_USERNAME/KAGGLE_KEY 环境变量）时直接拉取——最可靠。
2. 公开直链：Kaggle 对未登录浏览器放行的下载端点，无需账号；带重试与校验，
   失败时提示回退到方式 1（直链可能随 Kaggle 策略变化，属尽力而为）。

用法：
    python scripts/download_ecommerce.py                    # 下载到 ./data/kaggle-olist
    python scripts/download_ecommerce.py --dir ~/Downloads/olist
    python scripts/download_ecommerce.py --method kagglehub # 强制走 CLI
下载完成提示 import_ecommerce.py --csv-dir 导入。

固定校验：下载目录应含 CSV_SPECS 期望的 8 个 olist_*_dataset.csv（缺文件即报错，
不会让导入到一半才发现数据不完整）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

DATASET_SLUG = "olistbr/brazilian-ecommerce"
DATASET_NAME = "brazilian-ecommerce"
# 未登录浏览器可用的公开下载端点（与 Kaggle 页面"Download"按钮同源）
PUBLIC_DOWNLOAD_URL = f"https://www.kaggle.com/api/v1/datasets/download/{DATASET_SLUG}"

# import_ecommerce.CSV_SPECS 期望的官方文件名（完整集；导入只用其中 6 个，
# olist_geolocation/olist_order_reviews 是同包附带文件，一并校验防下载不完整）
EXPECTED_FILES = [
    "olist_customers_dataset.csv",
    "olist_geolocation_dataset.csv",
    "olist_order_items_dataset.csv",
    "olist_order_payments_dataset.csv",
    "olist_order_reviews_dataset.csv",
    "olist_orders_dataset.csv",
    "olist_products_dataset.csv",
    "olist_sellers_dataset.csv",
    "product_category_name_translation.csv",
]


def _verify(target_dir: Path) -> None:
    """校验下载完整性：期望文件必须齐。"""
    missing = [f for f in EXPECTED_FILES if not (target_dir / f).exists()]
    if missing:
        raise FileNotFoundError(f"下载不完整，缺少 {len(missing)} 个文件: {missing[:3]}...")


def _extract_flat(archive: Path, target_dir: Path) -> None:
    """解压压缩包并把文件平铺到 target_dir（Kaggle 包可能带一层目录）。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    extractor = zipfile.ZipFile if archive.suffix == ".zip" else tarfile.open
    with extractor(archive) as zf:  # type: ignore[operator]
        for member in zf.namelist() if isinstance(zf, zipfile.ZipFile) else zf.getnames():
            name = Path(member).name
            if not name or name.startswith("."):
                continue
            with zf.open(member) as src, (target_dir / name).open("wb") as dst:
                shutil.copyfileobj(src, dst)


def download_via_kagglehub(target_dir: Path) -> bool:
    """方式一：kagglehub / kaggle CLI（已配置凭证时最可靠）。"""
    try:
        import kagglehub
    except ImportError:
        try:
            from kaggle.api import KaggleApi  # noqa: F401 —— 老版 kaggle 包
        except ImportError:
            return False

    path = Path(kagglehub.dataset_download(DATASET_SLUG))
    # kagglehub 返回缓存目录（可能是目录或 zip）；统一平铺到 target_dir
    if path.is_file():
        _extract_flat(path, target_dir)
    else:
        target_dir.mkdir(parents=True, exist_ok=True)
        for f in path.rglob("*.csv"):
            shutil.copy2(f, target_dir / f.name)
    return True


def download_via_public(target_dir: Path, retries: int = 3) -> None:
    """方式二：公开直链（无需账号；Kaggle 策略变化时可能失效）。"""
    import time

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(PUBLIC_DOWNLOAD_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=120) as resp:  # noqa: S310 —— 公开数据集端点
                data = resp.read()
            archive = target_dir.parent / f"{DATASET_NAME}.zip"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(data)
            _extract_flat(archive, target_dir)
            archive.unlink(missing_ok=True)
            return
        except Exception as e:  # noqa: BLE001 —— 网络失败统一重试
            last_err = e
            print(f"  下载失败（第 {attempt}/{retries} 次）: {e}")
            time.sleep(2 * attempt)
    raise RuntimeError(
        f"公开直连下载失败: {last_err}\n"
        "回退方案：pip install kagglehub 后配置 Kaggle 凭证（~/.kaggle/kaggle.json 或 "
        "KAGGLE_USERNAME/KAGGLE_KEY），再跑 --method kagglehub；"
        f"或浏览器手动下载 {PUBLIC_DOWNLOAD_URL}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 Kaggle Brazilian E-Commerce 数据集")
    parser.add_argument("--dir", default="./data/kaggle-olist", help="下载目标目录（默认 ./data/kaggle-olist）")
    parser.add_argument("--method", choices=["auto", "kagglehub", "public"], default="auto")
    args = parser.parse_args(argv)

    target = Path(args.dir)
    if all((target / f).exists() for f in EXPECTED_FILES):
        print(f"目录已含完整数据集，跳过下载: {target}")
        return 0

    print(f"下载数据集 {DATASET_SLUG} → {target}")
    if args.method in ("auto", "kagglehub"):
        if download_via_kagglehub(target):
            print("已通过 kagglehub 下载")
        elif args.method == "kagglehub":
            raise RuntimeError("kagglehub 未安装或未配置凭证（见 kaggle.com/account 的 API token）")
    if args.method in ("auto", "public"):
        download_via_public(target)

    _verify(target)
    n = sum(1 for f in target.glob("*.csv"))
    print(f"完成：{n} 个 CSV 已就位于 {target}")
    print(f"导入演示库：python scripts/import_ecommerce.py --csv-dir {target} --db ./data/ecommerce.db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
