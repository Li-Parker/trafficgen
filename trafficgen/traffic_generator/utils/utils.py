import argparse


def get_parsed_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', '-c', type=str, default='local')
    parser.add_argument('--gif', action="store_true")
    parser.add_argument(
        '--save_metadrive',
        action="store_true",
        help="Whether to save generated scenarios to MetaDrive-compatible file.",
        default=True
    )
    args = parser.parse_args()
    return args
