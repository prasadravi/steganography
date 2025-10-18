import base64
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from PIL import Image, PngImagePlugin
import io

BLOCK_SIZE = 16

def pad(s: bytes) -> bytes:
    padding_len = BLOCK_SIZE - len(s) % BLOCK_SIZE
    return s + bytes([padding_len]) * padding_len

def unpad(s: bytes) -> bytes:
    padding_len = s[-1]
    return s[:-padding_len]

def aes_encrypt(key_bytes: bytes, plaintext: str) -> str:
    iv = get_random_bytes(16)
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(plaintext.encode('utf-8')))
    return base64.b64encode(iv + ct).decode('utf-8')

def aes_decrypt(key_bytes: bytes, ciphertext_b64: str) -> str:
    raw = base64.b64decode(ciphertext_b64.encode('utf-8'))
    iv = raw[:16]
    ct = raw[16:]
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    pt = unpad(cipher.decrypt(ct))
    return pt.decode('utf-8')

# Simple demo stego: write encrypted text into PNG tEXt chunk (demo only)
def embed_text_into_png(pil_image: Image.Image, encrypted_text: str) -> bytes:
    meta = PngImagePlugin.PngInfo()
    meta.add_text("stego", encrypted_text)
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG", pnginfo=meta)
    return buf.getvalue()

def extract_text_from_png(png_bytes: bytes) -> str:
    buf = io.BytesIO(png_bytes)
    im = Image.open(buf)
    info = im.info
    return info.get("stego", "")
