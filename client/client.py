# HACEMOS Y RECIIBIMOS SOLICITUDES HTTP

from client.commands import (
    ls,
    mkdir,
    rmdir,
    rm,
    send,
    change_directory
)


def main():
    current_path = "/"

    print("=" * 40)
    print("          DFSha Client")
    print("=" * 40)
    print()

    while True:
        try:
            user_input = input(
                f"DFSha:{current_path} > "
            ).strip()

        except (KeyboardInterrupt, EOFError):
            print("\nCerrando DFSha Client...")
            break

        if not user_input:
            continue

        parts = user_input.split()

        command = parts[0].lower()
        args = parts[1:]

        if command == "exit":
            print("Cerrando DFSha Client...")
            break

        elif command == "ls":
            ls(current_path)

        elif command == "pwd":
            print(current_path)

        elif command == "mkdir":
            if len(args) != 1:
                print("Uso: mkdir <directorio>")
                continue

            mkdir(current_path, args[0])

        elif command == "rmdir":
            if len(args) != 1:
                print("Uso: rmdir <directorio>")
                continue

            rmdir(current_path, args[0])

        elif command == "rm":
            if len(args) != 1:
                print("Uso: rm <archivo>")
                continue

            rm(current_path, args[0])

        elif command == "send":
            if len(args) != 1:
                print("Uso: send <archivo>")
                continue

            send(current_path, args[0])

        elif command == "cd":
            if len(args) != 1:
                print("Uso: cd <directorio>")
                continue

            current_path = change_directory(
                current_path,
                args[0]
            )

        else:
            print(f"Comando desconocido: {command}")


if __name__ == "__main__":
    main()