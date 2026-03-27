"""
core/log.py — Logging structuré JSON (Railway-friendly).

Chaque agent loggue avec son nom pour filtrage facile dans Railway :
  {"agent": "gmail", "task": "read", "status": "ok", "duration_ms": 342}
"""
import logging, json, time

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "level": record.levelname,
            "msg":   record.getMessage(),
            "name":  record.name,
        }
        if record.exc_info:
            log_obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, ensure_ascii=False)


def setup_logging(level=logging.INFO):
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
