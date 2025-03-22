import yaml
from pathlib import Path
import logging

LOGGER = logging.getLogger(__name__)


class Config:
    def __init__(self, conf_path, arg_to_conf_dict_path):
        with open(conf_path, 'rt', encoding='utf-8') as f:
            self.conf = yaml.safe_load(f)

        with open(arg_to_conf_dict_path, 'rt', encoding='utf-8') as f:
            self.arg_to_conf_dict = yaml.safe_load(f)

    def get_conf(self):
        return self.conf

    def set_by_key_path(self, key_name_path:str, value):
        key_names = key_name_path.split('.')
        conf_item = self.conf
        for index, key_name in enumerate(key_names):
            if index == len(key_names) - 1:
                conf_item[key_name] = value
            else:
                conf_item = conf_item[key_name]

    def override_conf(self, argparse_dict: dict):
        for argparse_name, argparse_value in argparse_dict.items():
            if argparse_name in self.arg_to_conf_dict:
                key_name_path = self.arg_to_conf_dict[argparse_name]
                self.set_by_key_path(key_name_path, argparse_value)


conf_path = Path(__file__).parent.parent / "conf.yaml"
arg_to_conf_dict_path = Path(__file__).parent.parent / "dict" / "arg_to_conf_dict.yaml"
conf = Config(conf_path, arg_to_conf_dict_path)
LOGGER.info("load conf, conf_path: %s, values: %s", conf_path, conf)