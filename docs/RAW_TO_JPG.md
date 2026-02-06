# RAW to JPG Stage

Converts proprietary RAW camera images to JPG through a two-step pipeline: RAW → DNG → JPG.

---

## `raw_to_jpg.py`

### Class: `RawToDng`

Converts RAW binary files to DNG

**Constructor: `__init__(self, cfg: dict)`**
- Extracts `color_matrix` and `dng_tags` from config
- Calls `_prepare_color_matrices()`

**Method: `_prepare_color_matrices(self) -> None`**
- Convert color matrices to DNG rational format
- Extracts color matrix, forward matrix, and white balance gains from numpy data
- Stores `ccm_rational` (color collection matrix), `fm_rational` (forward matrix), and `as_shot_neutral` (White Balance Neutral Values) as instance variables

**Method: `_create_dng_tags(self) -> DNGTags`**
- Creates `pidng.DNGTags` object with all DNG metadata
- Sets image dimensions, bit depth, CFA pattern, Bayer layout
- Sets camera metadata (make, model, serial, lens, focal length, aperture)
- Sets DNG core tags (version, black/white levels)
- Stores color calibration matrices (ColorMatrix1/2, ForwardMatrix1/2, AsShotNeutral)
- Sets illuminant and baseline exposure
- Returns fully configured DNGTags object

**Static Method: `_extract_timestamp_from_filename(filename: str) -> str`**
- Parses time from raw filetype
- Converts Unix epoch to converts to EXIF datetime format: `"YYYY:MM:DD HH:MM:SS"`
- Falls back to current UTC time if parsing fails

**Method: `convert(self, raw_path: Union[str, Path]) -> Path`**
- Main conversion method: RAW binary → DNG file
- Reads raw binary file as uint16 array (matches camera bit depth)
- Reshapes to camera dimensions (height × width from DNG tags)
- Creates DNG tags with `_create_dng_tags()`
- Adds timestamp metadata from filename
- Writes DNG file to `temp_dng_dir` using `pidng.RAW2DNG`
- Returns Path to created DNG file

---

### Class: `DngToJpg`

Develops DNG files to JPG format using RawTherapee CLI.

**Constructor: `__init__(self, cfg: Dict)`**
- Saves objects at paths `rawtherapee_cli`, `pp3_profile`, `rawtherapee_validate_script`
- Validates RawTherapee installation

**Method: `develop(self, dng_path: Path, jpg_path: Path, quality: int = 100) -> Path`**
- Turns DNG to JPG
- Builds `rawtherapee-cli` command with:
  - `-O`: output path
  - `-p`: PP3 processing profile
  - `-j{quality}`: JPEG quality (0-100)
  - `-js3`: chroma subsampling
  - `-Y`: overwrite existing files
  - `-c`: input DNG file
- Configures OpenMP threading environment variables:
  - `OMP_NUM_THREADS`: limits threads per instance (calculated for 12 parallel instances)
  - `OMP_DYNAMIC`: allows OpenMP to optimize thread usage
  - `OMP_NESTED`: disables nested parallelism
- Runs rawtherapee-cli as subprocess with 300 second timeout
- Returns Path to created JPG file

**Method: `validate_installation(self) -> bool`**
- Checks if RawTherapee CLI is accessible

**Method: `install_rawtherapee(self)`**
- Runs validation/setup script from config (`rawtherapee_validate_script`)

---

## `processor.py`

### Function: `load_config(config_path: Path) -> dict`

Loads camera configuration from YAML file and associated resources.

- Reads main config YAML with `yaml.safe_load()`
- If `paths.color_matrix` exists:
  - Finds color matrix from path
  - Loads numpy array with `np.load(matrix_path, allow_pickle=True)`
  - Stores in `config['color_matrix']`
- If `paths.svs_tags` exists:
  - Finds svg_tags from path
  - Loads YAML with DNG metadata structure
  - Stores in `config['dng_tags']`
- Returns unified config dictionary containing paths, color matrix, and DNG tags

---

### Class: `Processor`

High-level interface orchestrating RAW → JPG pipeline.

**Constructor: `__init__(self, config_path: Path)`**
- Loads config with `load_config(config_path)`
- Instantiates `RawToDng`. and `DngToJpg` with `config`
- Stores all as instance variables

**Method: `process_image(self, raw_path: Path, output_dir: Path) -> Path`**
- Processes a single RAW image to JPG with automatic cleanup
- Creates output directory if needed
- Calls `raw_to_dng.convert(raw_path)`
- Calls `dng_to_jpg.develop(dng_path, output_dir)`
- Handles conversion of raw to dng to jpg

**Method: `process_batch(self, raw_images: Iterable[Path], output_dir: Path, fail_stop: bool = True, max_workers: int = 0) -> List[Path]`**
- Processes multiple RAW images with optional parallelization
- **Sequential mode** (`max_workers <= 1`, default):
  - Iterates through raw_images and calls `process_image()` for each
- **Parallel mode** (`max_workers > 1`):
  - Creates ThreadPoolExecutor with specified worker count
  - Each thread calls `process_image()` for each image
- Returns list of successfully created JPG Paths

---

## `cli.py`

### Function: `log_batch_run(num_files: int, output_dir: Path, success: bool)`

Logs batch processing runs to local SQLite database.

- Creates `logs.db` if not exists
- Auto-creates table `stage_runs` with schema:
  - `id`: auto-incrementing primary key
  - `timestamp`: ISO format UTC timestamp
  - `num_files`: number of files processed
  - `output_dir`: output directory path as string
  - `success`: 1 or 0 (boolean as integer)
- Inserts record for current batch run
- Commits and closes connection

**Table Purpose**: Tracks historical batch processing for monitoring and debugging

---

### Function: `main()`

Command-line entry point for the raw_to_jpg stage.

**Arguments**:
- `--c`: Path to camera YAML configuration file (required)
- `--i`: Input directory containing RAW images (required)
- `--o`: Output directory for JPG images (required)
- `--t`: Number of parallel threads (default: 0 = sequential)
- `--fs`: Stop on first error when processing images, set to false by default



## Sample Run Commands
```sh
python3 -m stages.raw_to_jpg.cli \
  --c /home/btfarre2/checker/test.yaml \
  --i /mnt/research-projects/s/screberg/longterm_images2/semifield-upload/NC_2025-08-25/ \
  --o ./processed_jpgsg \
  --t 8
```


# Sample config yaml
```yaml
paths:
 color_matrix: /home/btfarre2/checker/MD_calibration_matrix_optimized.npy
 pp3_profile: /home/btfarre2/checker/MD_shr661_raw16.pp3
 temp_dng_dir: /home/btfarre2/agir_dng
 rawtherapee_cli: /home/btfarre2/tools/squashfs-root/usr/bin/rawtherapee-cli


dng_tags:
 image:
  SVCamImageWidth: 13376
  SVCamImageHeight: 9528
  BitsPerSample: 16
  PhotometricInterpretation: 32803
  Orientation: 1
  SamplesPerPixel: 1
  CFARepeatPatternDim: [2,2]
  CFAPattern: [0, 1, 1, 2]
  RowsPerStrip: 9528
  TileWidth: 0    # Set to 0 if unused
  TileLength: 0    # Set to 0 if unused

 camera:
  Make: SVS_VISTEK
  Model: shr661CXGE
  SerialNumber: "119885"
  LensModel: Linos Inspect XL 60mm
  FocalLength: 60
  FocalLengthIn35mmFilm: 46
  FNumber: 13
  FocalPlaneXResolution: 289.6
  FocalPlaneYResolution: 289.9
  FocalPlaneResolutionUnit: 3
  PixelSize: 3.45

 dng:
  DNGVersion: [1, 4, 0, 0]
  DNGBackwardVersion: [1, 2, 0, 0]
  BlackLevel: 368
  BlackLevel_12bit: 23
  WhiteLevel: 65535
  AsShotNeutral: [1,1,1]     # Neutral default
  BaselineExposure: [-150, 100]
  CalibrationIlluminant1: 21
  PreviewColorSpace: 2

 exif:
  TimeZoneOffset: -4
```