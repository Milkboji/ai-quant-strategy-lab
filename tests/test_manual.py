"""使用说明生成：在线文档导出的图片占位符换回截图、去掉 @提及。"""
import importlib
import sys
from pathlib import Path


def test_prepare_maps_image_placeholders(tmp_path):
    import pytest
    if not (Path(__file__).resolve().parent.parent / "docs" / "manual" / "images.json").exists():
        pytest.skip("公开仓库不含使用说明截图")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    bm = importlib.import_module("build_manual")
    md = ("# 标题\n\nOct 24, 2026 · @某人\n\n正文\n\n&#91;image: 登录页\\]\n\n&#91;image: 新加的图\\]\n")
    out = bm.prepare(md, tmp_path)
    assert "![登录页](img/01_login.jpg)" in out and (tmp_path / "img" / "01_login.jpg").exists()
    assert "（图：新加的图）" in out
    assert "@某人" not in out and "Oct 24, 2026" in out
