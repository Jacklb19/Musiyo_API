"""Inspect embedded GLB geometry and PNG/JPEG budgets without decoding meshes."""
import json
import struct
from pathlib import Path


def image_size(data: bytes, mime: str) -> tuple[int, int]:
    if mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", data[16:24])
    if mime == "image/jpeg" and data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 4 < len(data):
            if data[offset] != 255:
                raise ValueError("Invalid JPEG marker")
            marker = data[offset + 1]
            length = int.from_bytes(data[offset + 2:offset + 4], "big")
            if marker in {192, 193, 194}:
                height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
                return width, height
            if length < 2:
                break
            offset += 2 + length
    raise ValueError("Only valid embedded PNG/JPEG images are supported")


def audit_glb(path: Path, profile: str) -> dict:
    data = path.read_bytes()
    if len(data) < 20 or data[:8] != b"glTF\x02\x00\x00\x00" or struct.unpack_from("<I", data, 8)[0] != len(data):
        raise ValueError("Invalid GLB container")
    offset, document, binary = 12, None, b""
    while offset + 8 <= len(data):
        length, kind = struct.unpack_from("<II", data, offset)
        chunk = data[offset + 8:offset + 8 + length]
        if len(chunk) != length:
            raise ValueError("Truncated GLB chunk")
        if kind == 0x4E4F534A:
            document = json.loads(chunk)
        elif kind == 0x004E4942:
            binary = chunk
        offset += length + 8
    if document is None or offset != len(data):
        raise ValueError("Missing GLB JSON")
    if any(buffer.get("uri") for buffer in document.get("buffers", [])) or any(image.get("uri") for image in document.get("images", [])):
        raise ValueError("External model dependencies are not supported")
    triangles = 0
    for mesh in document.get("meshes", []):
        for primitive in mesh["primitives"]:
            if primitive.get("mode", 4) != 4:
                raise ValueError("Only triangle meshes are supported")
            accessor = primitive.get("indices", primitive["attributes"]["POSITION"])
            count = document["accessors"][accessor]["count"]
            if count % 3:
                raise ValueError("Invalid triangle count")
            triangles += count // 3
    textures = []
    for image in document.get("images", []):
        view = document["bufferViews"][image["bufferView"]]
        if view.get("buffer", 0) != 0:
            raise ValueError("Unexpected image buffer")
        start = view.get("byteOffset", 0)
        width, height = image_size(binary[start:start + view["byteLength"]], image.get("mimeType", ""))
        textures.append({"width": width, "height": height, "mime": image["mimeType"]})
    limits = {"quest": (6_000_000, 40_000, 1024), "web": (10_000_000, 130_000, 2048)}[profile]
    if not 0 < triangles <= limits[1] or len(data) > limits[0] or any(max(t["width"], t["height"]) > limits[2] for t in textures):
        raise ValueError("Model exceeds its geometry, byte or texture budget")
    return {"byte_count": len(data), "triangles": triangles, "materials": len(document.get("materials", [])), "textures": textures,
        "extensions_required": document.get("extensionsRequired", []), "decoded_geometry_verified": False}
