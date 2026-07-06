import logging

class LoggerSetup:
    """Configura e restituisce l'istanza globale del logger."""

    @staticmethod
    def configure(debug_mode: bool) -> logging.Logger:
        logger = logging.getLogger("ScariMusicBot")
        logger.setLevel(logging.DEBUG if debug_mode else logging.WARNING)

        # Evita di aggiungere handler duplicati se il metodo viene chiamato più volte
        if not logger.handlers:
            fmt_str = "%(asctime)s - %(levelname)s: %(message)s" if debug_mode else "%(levelname)s: %(message)s"
            formatter = logging.Formatter(fmt_str, datefmt="%H:%M:%S")

            # Handler per la console
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

            # Handler per il file di log (scrittura su file system)
            file_handler = logging.FileHandler("execution.log", encoding="utf-8", mode="w")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        return logger