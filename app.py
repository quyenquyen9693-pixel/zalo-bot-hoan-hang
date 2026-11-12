"""Zalo bot: đọc mã QR/vạch trên ảnh đơn hoàn và trả về text.

Luồng:
- Nhân viên gửi ảnh (nhắn riêng với bot).
- Bot đọc mã, trả về mã kèm ngày ghi nhận, rồi hỏi tình trạng:
    1 = Đủ, 2 = Hư hỏng/Thiếu
- Trả lời xong mới xử lý ảnh tiếp theo.
- Không lưu dữ liệu. Trạng thái chờ xác nhận chỉ nằm trong bộ nhớ.
"""

import io
import os
import threading
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import zxingcpp
from flask import Flask, abort, request
from PIL import Image

BOT_TOKEN = os.environ["ZALO_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["ZALO_WEBHOOK_SECRET"]
API = f"https://bot-api.zaloplatforms.com/bot{BOT_TOKEN}"
TZ = ZoneInfo("Asia/Ho_Chi_Minh")

app = Flask(__name__)
lock = threading.Lock()
# Mỗi chat: hàng đợi ảnh chờ xử lý và đơn đang chờ nhân viên xác nhận.
states: dict[str, dict] = {}

PROMPT = "Tình trạng hàng?\n1 = Đủ\n2 = Hư hỏng/Thiếu"


def send_text(chat_id: str, text: str) -> None:
    requests.post(
        f"{API}/sendMessage",
        json={"chat_id": chat_id, "text": text[:2000]},
        timeout=15,
    )


def get_state(chat_id: str) -> dict:
    return states.setdefault(chat_id, {"queue": deque(), "pending": None})


def download_image(url: str) -> Image.Image:
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def read_codes(img: Image.Image) -> list[str]:
    return [b.text for b in zxingcpp.read_barcodes(img) if b.text]


def process_next(chat_id: str) -> None:
    """Xử lý ảnh tiếp theo trong hàng đợi, chỉ khi không có đơn nào đang chờ."""
    st = get_state(chat_id)
    while st["queue"] and st["pending"] is None:
        url = st["queue"].popleft()
        try:
            img = download_image(url)
        except Exception:
            send_text(chat_id, "Không tải được ảnh, vui lòng gửi lại.")
            continue

        codes = read_codes(img)
        if not codes:
            send_text(chat_id, "Không đọc được mã trên ảnh, vui lòng chụp lại rõ hơn.")
            continue

        date = datetime.now(TZ).strftime("%d/%m/%Y")
        if len(codes) == 1:
            # TODO: đổi nhãn khi có ảnh mẫu, để biết mã nào là đơn, mã nào là vận đơn.
            lines = [f"Mã: {codes[0]}"]
        else:
            lines = [f"Mã 1: {codes[0]}", f"Mã 2: {codes[1]}"]
            lines += [f"Mã {i + 1}: {c}" for i, c in enumerate(codes[2:], start=2)]

        st["pending"] = {"codes": codes, "date": date}
        send_text(chat_id, "\n".join(lines + [f"Ngày ghi nhận: {date}", "", PROMPT]))


def handle_image(chat_id: str, url: str) -> None:
    with lock:
        st = get_state(chat_id)
        st["queue"].append(url)
        if st["pending"] is not None:
            send_text(chat_id, "Đang chờ xác nhận đơn trước, ảnh này đã xếp hàng.")
            return
        process_next(chat_id)


def handle_text(chat_id: str, text: str) -> None:
    with lock:
        st = get_state(chat_id)
        answer = text.strip()
        if st["pending"] is None:
            send_text(chat_id, "Vui lòng gửi ảnh đơn hoàn.")
            return
        if answer not in ("1", "2"):
            send_text(chat_id, "Vui lòng trả lời 1 (Đủ) hoặc 2 (Hư hỏng/Thiếu).")
            return

        pending = st["pending"]
        st["pending"] = None
        if answer == "1":
            send_text(chat_id, "Đã ghi nhận: Đủ hàng.")
        else:
            codes = ", ".join(pending["codes"])
            send_text(
                chat_id,
                f"Đã ghi nhận: Hư hỏng/Thiếu ({codes}). Vui lòng báo lên quản lý.",
            )
        process_next(chat_id)


def get_chat_id(msg: dict) -> str | None:
    # TODO: xác nhận cấu trúc payload khi có sự kiện thật.
    return (msg.get("chat") or {}).get("id") or msg.get("chat_id")


def get_photo_url(msg: dict) -> str | None:
    # Tài liệu mô tả khác nhau về tên trường, thử cả hai.
    return msg.get("photo_url") or msg.get("photo")


def dispatch(data: dict) -> None:
    event = data.get("event_name")
    msg = data.get("message") or {}
    chat_id = get_chat_id(msg)
    if not chat_id:
        return

    if event == "message.image.received":
        url = get_photo_url(msg)
        if url:
            handle_image(chat_id, url)
    elif event == "message.text.received":
        handle_text(chat_id, msg.get("text", ""))


@app.post("/webhook")
def webhook():
    if request.headers.get("X-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)
    data = request.get_json(silent=True) or {}
    # Trả 200 ngay, xử lý nền để Zalo không timeout.
    threading.Thread(target=dispatch, args=(data,), daemon=True).start()
    return "ok", 200


@app.get("/")
def health():
    return "ok", 200
