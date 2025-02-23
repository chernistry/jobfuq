# src/logger.py

import logging
import sys

def setup_logger(verbose=False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        stream=sys.stdout
    )
    return logging.getLogger(__name__)

logger = setup_logger()

def set_verbose(verbose):
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)