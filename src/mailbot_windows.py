import base64
import ctypes
import os


def windows_password(data, *, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("IMAP 비밀번호 저장에는 Windows가 필요합니다.")

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.c_void_p)]

    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.addressof(buffer))
    output = DataBlob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    convert = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    convert.argtypes = (
        ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DataBlob),
    )
    convert.restype = ctypes.c_int
    if not convert(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    release = ctypes.WinDLL("kernel32").LocalFree
    release.argtypes = (ctypes.c_void_p,)
    release.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        release(output.data)


def protect_password(password):
    return base64.b64encode(windows_password(password.encode("utf-8"))).decode("ascii")


def unlock_password(protected):
    return windows_password(base64.b64decode(protected, validate=True), decrypt=True).decode("utf-8")


def powershell_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def powershell_command(script):
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def background_stop_path(config):
    return config.with_suffix(".background-stop")


def execution_lock(config):
    import msvcrt

    lock = config.with_suffix(".lock").open("a+b")
    try:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        raise RuntimeError("같은 설정으로 MailBot이 이미 실행 중입니다. 기존 창을 닫거나 자동 실행을 끄세요.") from None
    return lock
