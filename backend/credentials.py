import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialStore:
    def __init__(self, path, allow_create=False):
        path = Path(path)
        if not path.exists():
            if not allow_create:
                raise RuntimeError('Chave de credenciais ausente. Restaure a chave original antes de iniciar.')
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(Fernet.generate_key())
        self.cipher = Fernet(path.read_bytes().strip())

    def encrypt(self, value):
        return self.cipher.encrypt(value.encode('utf-8')).decode('ascii')

    def decrypt(self, value):
        try:
            return self.cipher.decrypt(value.encode('ascii')).decode('utf-8')
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise RuntimeError('Chave de credenciais incorreta ou credencial corrompida.') from exc
