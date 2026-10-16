# backend_fastapi/routers/upload.py
"""
上传路由，对应原 backend/routes/upload.py。

2 个接口：
  POST /api/upload                  文件上传
  GET  /api/uploads/{filename}      静态文件读取

与 Flask 版差异：
  - request.files['file']  → UploadFile = File(...)
  - file.save(path)        → shutil.copyfileobj(file.file, f)
  - send_from_directory()  → FileResponse()
  - 手动做目录穿越防护（basename 剥离路径）
"""
import os
import shutil
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from werkzeug.utils import secure_filename

from config import UPLOAD_FOLDER

router = APIRouter()

# 与原 Flask 一致
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "svg"}

# 首次导入时确保目录存在（与原 Flask 的 os.makedirs 一致）
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def allowed_file(filename: str) -> bool:
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


# ==================================================================
# POST /api/upload
# ==================================================================
@router.post("/upload")
def upload_file(file: UploadFile = File(...)):
    if not file or not file.filename:
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "No selected file"},
        )

    if not allowed_file(file.filename):
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "不支持的文件类型"},
        )

    # 生成安全的文件名，加 UUID 前缀防止冲突（与原 Flask 完全一致）
    filename = secure_filename(file.filename)
    unique_filename = f"{uuid.uuid4().hex}_{filename}"

    dest_path = os.path.join(UPLOAD_FOLDER, unique_filename)

    # 用 shutil.copyfileobj 流式写盘（避免大文件占用内存）
    with open(dest_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    file_url = f"/api/uploads/{unique_filename}"

    return {
        "status": 0,
        "msg": "上传成功",
        "data": {"url": file_url, "name": filename},
    }


# ==================================================================
# GET /api/uploads/{filename}
# ==================================================================
@router.get("/uploads/{filename}")
def uploaded_file(filename: str):
    # 目录穿越防护：只取 basename
    safe_name = os.path.basename(filename)
    file_path = os.path.join(UPLOAD_FOLDER, safe_name)

    if not os.path.isfile(file_path):
        raise HTTPException(
            status_code=404,
            detail={"status": 1, "msg": "文件不存在"},
        )

    return FileResponse(file_path)