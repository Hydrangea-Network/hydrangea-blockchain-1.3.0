from dataclasses import dataclass
import logging
import threading
import time
import traceback
from multiprocessing.pool import Pool
from os import stat_result
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, ItemsView, ValuesView, KeysView
from concurrent.futures import Future
from concurrent.futures.thread import ThreadPoolExecutor

from blspy import G1Element
from chiapos import DiskProver

from chia.consensus.pos_quality import UI_ACTUAL_SPACE_CONSTANT_FACTOR, _expected_plot_size
from chia.plotting.util import (
    PlotInfo,
    PlotRefreshResult,
    PlotsRefreshParameter,
    PlotRefreshEvents,
    get_plot_filenames,
    parse_plot_info,
)
from chia.util.generator_tools import list_to_batches
from chia.util.ints import uint16, uint64
from chia.util.path import mkdir
from chia.util.streamable import Streamable, streamable
from chia.types.blockchain_format.proof_of_space import ProofOfSpace
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.wallet.derive_keys import master_sk_to_local_sk

log = logging.getLogger(__name__)

CURRENT_VERSION: int = 1


@dataclass(frozen=True)
@streamable
class CacheKeys(Streamable):
    farmer_public_key: G1Element
    pool_public_key: Optional[G1Element]
    pool_contract_puzzle_hash: Optional[bytes32]
    plot_public_key: G1Element


@dataclass(frozen=True)
@streamable
class DiskCacheEntry(Streamable):
    prover_data: bytes
    keys: CacheKeys
    last_use: uint64


@dataclass(frozen=True)
@streamable
class DiskCache(Streamable):
    version: uint16
    data: List[Tuple[str, DiskCacheEntry]]


@dataclass
class CacheEntry:
    prover: DiskProver
    keys: CacheKeys
    last_use: float

    def bump_last_use(self) -> None:
        self.last_use = time.time()

    def expired(self, expiry_seconds: int) -> bool:
        return time.time() - self.last_use > expiry_seconds


class Cache:
    _changed: bool
    _data: Dict[Path, CacheEntry]
    expiry_seconds: int = 7 * 24 * 60 * 60  # Keep the cache entries alive for 7 days after its last access

    def __init__(self, path: Path) -> None:
        self._changed = False
        self._data = {}
        self._path = path
        if not path.parent.exists():
            mkdir(path.parent)

    def __len__(self) -> int:
        return len(self._data)

    def update(self, path: Path, entry: CacheEntry) -> None:
        self._data[path] = entry
        self._changed = True

    def remove(self, cache_keys: List[Path]) -> None:
        for key in cache_keys:
            if key in self._data:
                del self._data[key]
                self._changed = True

    def save(self) -> None:
        try:
            disk_cache_entries: Dict[str, DiskCacheEntry] = {
                str(path): DiskCacheEntry(
                    bytes(cache_entry.prover),
                    cache_entry.keys,
                    uint64(int(cache_entry.last_use)),
                )
                for path, cache_entry in self.items()
            }
            disk_cache: DiskCache = DiskCache(
                uint16(CURRENT_VERSION), [(plot_id, cache_entry) for plot_id, cache_entry in disk_cache_entries.items()]
            )
            serialized: bytes = bytes(disk_cache)
            self._path.write_bytes(serialized)
            self._changed = False
            log.info(f"Saved {len(serialized)} bytes of cached data")
        except Exception as e:
            log.error(f"Failed to save cache: {e}, {traceback.format_exc()}")

    def load(self) -> None:
        try:
            serialized = self._path.read_bytes()
            version = uint16.from_bytes(serialized[0:2])
            log.info(f"Loaded {len(serialized)} bytes of cached data")
            if version == CURRENT_VERSION:
                stored_cache: DiskCache = DiskCache.from_bytes(serialized)
                self._data = {
                    Path(path): CacheEntry(
                        DiskProver.from_bytes(cache_entry.prover_data),
                        cache_entry.keys,
                        float(cache_entry.last_use),
                    )
                    for path, cache_entry in stored_cache.data
                }
            else:
                raise ValueError(f"Invalid cache version {version}. Expected version {CURRENT_VERSION}.")
        except FileNotFoundError:
            log.debug(f"Cache {self._path} not found")
        except Exception as e:
            log.error(f"Failed to load cache: {e}, {traceback.format_exc()}")

    def keys(self) -> KeysView[Path]:
        return self._data.keys()

    def values(self) -> ValuesView[CacheEntry]:
        return self._data.values()

    def items(self) -> ItemsView[Path, CacheEntry]:
        return self._data.items()

    def get(self, path: Path) -> Optional[CacheEntry]:
        return self._data.get(path)

    def changed(self) -> bool:
        return self._changed

    def path(self) -> Path:
        return self._path


@dataclass
class PreProcessingResult:
    path: Path
    cache_entry: Optional[CacheEntry]
    stat_info: Optional[stat_result]
    prover: Optional[DiskProver]
    duration: float


class PlotManager:
    plots: Dict[Path, PlotInfo]
    plot_filename_paths: Dict[str, Tuple[str, Set[str]]]
    plot_filename_paths_lock: threading.Lock
    failed_to_open_filenames: Dict[Path, int]
    no_key_filenames: Set[Path]
    farmer_public_keys: List[G1Element]
    pool_public_keys: List[G1Element]
    cache: Cache
    match_str: Optional[str]
    show_memo: bool
    open_no_key_filenames: bool
    last_refresh_time: float
    refresh_parameter: PlotsRefreshParameter
    log: Any
    _lock: threading.Lock
    _refresh_thread: Optional[threading.Thread]
    _refreshing_enabled: bool
    _refresh_callback: Callable
    _initial: bool
    _thread_pool: ThreadPoolExecutor
    _process_pool: Pool

    def __init__(
        self,
        root_path: Path,
        refresh_callback: Callable,
        match_str: Optional[str] = None,
        show_memo: bool = False,
        open_no_key_filenames: bool = False,
        refresh_parameter: PlotsRefreshParameter = PlotsRefreshParameter(),
    ):
        self.root_path = root_path
        self.plots = {}
        self.plot_filename_paths = {}
        self.plot_filename_paths_lock = threading.Lock()
        self.failed_to_open_filenames = {}
        self.no_key_filenames = set()
        self.farmer_public_keys = []
        self.pool_public_keys = []
        self.cache = Cache(self.root_path.resolve() / "cache" / "plot_manager.dat")
        self.match_str = match_str
        self.show_memo = show_memo
        self.open_no_key_filenames = open_no_key_filenames
        self.last_refresh_time = 0
        self.refresh_parameter = refresh_parameter
        self.log = logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._refresh_thread = None
        self._refreshing_enabled = False
        self._refresh_callback = refresh_callback  # type: ignore
        self._initial = True
        self._thread_pool = ThreadPoolExecutor()
        self._process_pool = Pool()

    def __enter__(self):
        self._lock.acquire()

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self._lock.release()

    def reset(self):
        with self:
            self.last_refresh_time = time.time()
            self.plots.clear()
            self.plot_filename_paths.clear()
            self.failed_to_open_filenames.clear()
            self.no_key_filenames.clear()
            self._initial = True

    def set_refresh_callback(self, callback: Callable):
        self._refresh_callback = callback  # type: ignore

    def set_public_keys(self, farmer_public_keys: List[G1Element], pool_public_keys: List[G1Element]):
        self.farmer_public_keys = farmer_public_keys
        self.pool_public_keys = pool_public_keys

    def initial_refresh(self):
        return self._initial

    def public_keys_available(self):
        return len(self.farmer_public_keys) and len(self.pool_public_keys)

    def plot_count(self):
        with self:
            return len(self.plots)

    def get_duplicates(self):
        result = []
        for plot_filename, paths_entry in self.plot_filename_paths.items():
            _, duplicated_paths = paths_entry
            for path in duplicated_paths:
                result.append(Path(path) / plot_filename)
        return result

    def needs_refresh(self) -> bool:
        return time.time() - self.last_refresh_time > float(self.refresh_parameter.interval_seconds)

    def start_refreshing(self):
        self._refreshing_enabled = True
        if self._refresh_thread is None or not self._refresh_thread.is_alive():
            self.cache.load()
            self._refresh_thread = threading.Thread(target=self._refresh_task)
            self._refresh_thread.start()

    def stop_refreshing(self):
        self._refreshing_enabled = False
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            self._refresh_thread.join()
            self._refresh_thread = None

    def trigger_refresh(self):
        log.debug("trigger_refresh")
        self.last_refresh_time = 0

    def _refresh_task(self):
        while self._refreshing_enabled:
            try:
                while not self.needs_refresh() and self._refreshing_enabled:
                    time.sleep(1)

                if not self._refreshing_enabled:
                    return

                plot_filenames: Dict[Path, List[Path]] = get_plot_filenames(self.root_path)
                # plot_directories: Set[Path] = set(plot_filenames.keys())
                plot_paths: List[Path] = []
                for paths in plot_filenames.values():
                    plot_paths += paths

                total_result: PlotRefreshResult = PlotRefreshResult()
                total_size = len(plot_paths)

                self._refresh_callback(PlotRefreshEvents.started, PlotRefreshResult(remaining=total_size))

                # First drop all plots we have in plot_filename_paths but not longer in the filesystem or set in config
                for path in list(self.failed_to_open_filenames.keys()):
                    if path not in plot_paths:
                        del self.failed_to_open_filenames[path]

                for path in self.no_key_filenames.copy():
                    if path not in plot_paths:
                        self.no_key_filenames.remove(path)

                with self:
                    filenames_to_remove: List[str] = []
                    for plot_filename, paths_entry in self.plot_filename_paths.items():
                        loaded_path, duplicated_paths = paths_entry
                        loaded_plot = Path(loaded_path) / Path(plot_filename)
                        if loaded_plot not in plot_paths:
                            filenames_to_remove.append(plot_filename)
                            if loaded_plot in self.plots:
                                del self.plots[loaded_plot]
                            total_result.removed.append(loaded_plot)
                            # No need to check the duplicates here since we drop the whole entry
                            continue

                        paths_to_remove: List[str] = []
                        for path in duplicated_paths:
                            loaded_plot = Path(path) / Path(plot_filename)
                            if loaded_plot not in plot_paths:
                                paths_to_remove.append(path)
                                total_result.removed.append(loaded_plot)
                        for path in paths_to_remove:
                            duplicated_paths.remove(path)

                    for filename in filenames_to_remove:
                        del self.plot_filename_paths[filename]

                for remaining, futures in self.pre_process_files(plot_paths):
                    # Collect pre processing results first
                    batch_result: PlotRefreshResult = self.refresh_batch([f.result() for f in futures])
                    if not self._refreshing_enabled:
                        self.log.debug("refresh_plots: Aborted")
                        break
                    # Set the remaining files since `refresh_batch()` doesn't know them but we want to report it
                    batch_result.remaining = remaining
                    total_result.loaded += batch_result.loaded
                    total_result.processed += batch_result.processed
                    total_result.duration += batch_result.duration

                    self._refresh_callback(PlotRefreshEvents.batch_processed, batch_result)
                    if remaining == 0:
                        break

                if self._refreshing_enabled:
                    self._refresh_callback(PlotRefreshEvents.done, total_result)

                # Reset the initial refresh indication
                self._initial = False

                # Cleanup unused cache
                self.log.debug(f"_refresh_task: cached entries before cleanup: {len(self.cache)}")
                remove_paths: List[Path] = []
                for path, cache_entry in self.cache.items():
                    if cache_entry.expired(Cache.expiry_seconds) and path not in self.plots:
                        remove_paths.append(path)
                    elif path in self.plots:
                        cache_entry.bump_last_use()
                self.cache.remove(remove_paths)
                self.log.debug(f"_refresh_task: cached entries removed: {len(remove_paths)}")

                if self.cache.changed():
                    self.cache.save()

                self.last_refresh_time = time.time()

                self.log.debug(
                    f"_refresh_task: total_result.loaded {len(total_result.loaded)}, "
                    f"total_result.removed {len(total_result.removed)}, "
                    f"total_duration {total_result.duration:.2f} seconds"
                )
            except Exception as e:
                log.error(f"_refresh_callback raised: {e} with the traceback: {traceback.format_exc()}")
                self.reset()

    def pre_process_file(self, file_path: Path) -> PreProcessingResult:
        result: PreProcessingResult = PreProcessingResult(file_path, None, None, None, 0.0)
        pre_processing_start: float = time.time()
        if not self._refreshing_enabled:
            return result
        filename_str = str(file_path)
        if self.match_str is not None and self.match_str not in filename_str:
            return result
        if (
            file_path in self.failed_to_open_filenames
            and (time.time() - self.failed_to_open_filenames[file_path]) < self.refresh_parameter.retry_invalid_seconds
        ):
            # Try once every `refresh_parameter.retry_invalid_seconds` seconds to open the file
            return result

        if file_path in self.plots:
            return result

        entry: Optional[Tuple[str, Set[str]]] = self.plot_filename_paths.get(file_path.name)
        if entry is not None:
            loaded_parent, duplicates = entry
            if str(file_path.parent) in duplicates:
                log.debug(f"Skip duplicated plot {str(file_path)}")
                return result

        try:
            stat_info = file_path.stat()
            prover = DiskProver(str(file_path))
        except Exception as e:
            tb = traceback.format_exc()
            log.error(f"Failed to open file {file_path}. {e} {tb}")
            self.failed_to_open_filenames[file_path] = int(time.time())
            return result

        expected_size = _expected_plot_size(prover.get_size()) * UI_ACTUAL_SPACE_CONSTANT_FACTOR

        # TODO: consider checking if the file was just written to (which would mean that the file is still
        # being copied). A segfault might happen in this edge case.

        if prover.get_size() >= 30 and stat_info.st_size < 0.98 * expected_size:
            log.warning(
                f"Not farming plot {file_path}. Size is {stat_info.st_size / (1024 ** 3)} GiB, but expected"
                f" at least: {expected_size / (1024 ** 3)} GiB. We assume the file is being copied."
            )
            return result

        result.cache_entry = self.cache.get(file_path)
        result.stat_info = stat_info
        result.prover = prover
        result.duration = time.time() - pre_processing_start

        return result

    def pre_process_files(self, file_paths: List[Path]) -> List[Tuple[int, List[Future]]]:
        results: List[Tuple[int, List[Future]]] = []
        for remaining, batch in list_to_batches(file_paths, self.refresh_parameter.batch_size):
            results.append((remaining, [self._thread_pool.submit(self.pre_process_file, path) for path in batch]))
        return results

    @staticmethod
    def process_memo(file_path: Path, memo: bytes) -> Tuple[Path, bytes]:
        log.debug(f"process_memo {str(file_path)}")

        (
            pool_public_key_or_puzzle_hash,
            farmer_public_key,
            local_master_sk,
        ) = parse_plot_info(memo)

        pool_public_key: Optional[G1Element] = None
        pool_contract_puzzle_hash: Optional[bytes32] = None
        if isinstance(pool_public_key_or_puzzle_hash, G1Element):
            pool_public_key = pool_public_key_or_puzzle_hash
        else:
            assert isinstance(pool_public_key_or_puzzle_hash, bytes32)
            pool_contract_puzzle_hash = pool_public_key_or_puzzle_hash

        local_sk = master_sk_to_local_sk(local_master_sk)

        plot_public_key: G1Element = ProofOfSpace.generate_plot_public_key(
            local_sk.get_g1(), farmer_public_key, pool_contract_puzzle_hash is not None
        )

        keys: CacheKeys = CacheKeys(farmer_public_key, pool_public_key, pool_contract_puzzle_hash, plot_public_key)

        return file_path, bytes(keys)

    def post_process_file(self, file_path: Path, stat_info: stat_result, cache_entry: CacheEntry) -> Optional[PlotInfo]:
        # Only use plots that correct keys associated with them
        if cache_entry.keys.farmer_public_key not in self.farmer_public_keys:
            log.warning(f"Plot {file_path} has a farmer public key that is not in the farmer's pk list.")
            self.no_key_filenames.add(file_path)
            if not self.open_no_key_filenames:
                return None

        if (
            cache_entry.keys.pool_public_key is not None
            and cache_entry.keys.pool_public_key not in self.pool_public_keys
        ):
            log.warning(f"Plot {file_path} has a pool public key that is not in the farmer's pool pk list.")
            self.no_key_filenames.add(file_path)
            if not self.open_no_key_filenames:
                return None

        # If a plot is in `no_key_filenames` the keys were missing in earlier refresh cycles. We can remove
        # the current plot from that list if its in there since we passed the key checks above.
        if file_path in self.no_key_filenames:
            self.no_key_filenames.remove(file_path)

        with self.plot_filename_paths_lock:
            paths: Optional[Tuple[str, Set[str]]] = self.plot_filename_paths.get(file_path.name)
            if paths is None:
                paths = (str(Path(cache_entry.prover.get_filename()).parent), set())
                self.plot_filename_paths[file_path.name] = paths
            else:
                paths[1].add(str(Path(cache_entry.prover.get_filename()).parent))
                log.warning(f"Have multiple copies of the plot {file_path.name} in {[paths[0], *paths[1]]}.")
                return None

        new_plot_info: PlotInfo = PlotInfo(
            cache_entry.prover,
            cache_entry.keys.pool_public_key,
            cache_entry.keys.pool_contract_puzzle_hash,
            cache_entry.keys.plot_public_key,
            stat_info.st_size,
            stat_info.st_mtime,
        )

        cache_entry.bump_last_use()

        if file_path in self.failed_to_open_filenames:
            del self.failed_to_open_filenames[file_path]

        log.info(f"Found plot {file_path} of size {new_plot_info.prover.get_size()}")

        return new_plot_info

    def refresh_batch(self, pre_processing_results) -> PlotRefreshResult:
        start_time: float = time.time()
        result: PlotRefreshResult = PlotRefreshResult(processed=len(pre_processing_results))
        plots_refreshed: Dict[Path, PlotInfo] = {}
        new_plot_info: Optional[PlotInfo]

        log.debug(f"refresh_batch: {len(pre_processing_results)} files in directories")

        if self.match_str is not None:
            log.info(f'Only loading plots that contain "{self.match_str}" in the file or directory name')

        process_args: List[Tuple[Path, bytes]] = []
        stat_infos: Dict[Path, stat_result] = {}
        provers: Dict[Path, DiskProver] = {}
        for pre_result in pre_processing_results:
            if pre_result.stat_info is not None:
                if pre_result.cache_entry is None:
                    assert pre_result.prover is not None
                    process_args.append((pre_result.path, pre_result.prover.get_memo()))
                    stat_infos[pre_result.path] = pre_result.stat_info
                    provers[pre_result.path] = pre_result.prover
                else:
                    new_plot_info = self.post_process_file(
                        pre_result.path, pre_result.stat_info, pre_result.cache_entry
                    )
                    if new_plot_info is not None:
                        plots_refreshed[pre_result.path] = new_plot_info
                        result.loaded.append(new_plot_info)

        process_memos_start: float = time.time()
        process_memos_results = self._process_pool.starmap(PlotManager.process_memo, process_args)
        process_memos_duration: float = time.time() - process_memos_start
        for path, result_bytes in process_memos_results:

            if result_bytes is None:
                self.failed_to_open_filenames[path] = int(time.time())
            else:
                keys: CacheKeys = CacheKeys.from_bytes(result_bytes)
                cache_entry = CacheEntry(
                    provers[path],
                    keys,
                    time.time(),
                )
                self.cache.update(path, cache_entry)
                new_plot_info = self.post_process_file(path, stat_infos[path], cache_entry)
                if new_plot_info is not None:
                    plots_refreshed[path] = new_plot_info
                    result.loaded.append(new_plot_info)

        update_plots_start: float = time.time()
        with self:
            update_plots_locked: float = time.time() - update_plots_start
            self.plots.update(plots_refreshed)
            update_plots_duration: float = time.time() - update_plots_start

        pre_processing_duration = sum(x.duration for x in pre_processing_results)
        result.duration = time.time() - start_time + pre_processing_duration

        self.log.debug(
            f"refresh_batch: loaded {len(result.loaded)}, "
            f"removed {len(result.removed)}, processed {result.processed}, "
            f"remaining {result.remaining}, batch_size {self.refresh_parameter.batch_size}, "
            f"update_plots_locked: {update_plots_locked:.2f} seconds,"
            f"update_plots_duration: {update_plots_duration:.2f} seconds, "
            f"pre_processing: {pre_processing_duration:.2f} seconds, "
            f"process_memos: {process_memos_duration:.2f} seconds, "
            f"duration: {result.duration:.2f} seconds"
        )
        return result
