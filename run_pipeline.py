import sys
import yaml
from pathlib import Path
from src.artifact_cleaning import silence_saturation_events, remove_comb_noise

def main():
    config_path = Path("config/config.yaml")
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)
        
    print(f"[1/4] Loaded config. Raw data directory: {cfg['paths']['raw_data_dir']}")
    print(f"[2/4] Initializing artifact cleaning on target channels: {cfg['artifact_filters']['comb_filter_channels']}")
    # Pipeline calls to AP and LFP submodules execution go here...
    print("[3/4] Running Kilosort4 spike sorting...")
    print("[4/4] Extracting continuous LFP stream...")
    print("Processing complete.")

if __name__ == "__main__":
    main()