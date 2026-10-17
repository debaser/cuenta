"""Command line for the admin panel (run it inside the container):

    docker exec -it cuenta python admin_tools.py set-password   # asks twice, never takes the password as an argument
    docker exec -it cuenta python admin_tools.py status
    docker exec -it cuenta python admin_tools.py logout-all     # closes every admin session
"""
import getpass
import sys

import adminauth
import store


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    store.init()
    if cmd == "set-password":
        pw = getpass.getpass("Nueva contraseña del panel (mínimo 8 caracteres): ")
        if len(pw) < 8:
            print("Demasiado corta: usa al menos 8 caracteres.")
            return 1
        if getpass.getpass("Repítela: ") != pw:
            print("No coinciden.")
            return 1
        store.admin_hash_set(adminauth.hash_password(pw))
        print("Contraseña guardada. Se han cerrado las sesiones abiertas.")
        return 0
    if cmd == "status":
        print("Contraseña del panel:", "configurada" if store.admin_hash_get() else "SIN CONFIGURAR")
        return 0
    if cmd == "logout-all":
        store.drop_all_sessions()
        print("Sesiones cerradas.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
