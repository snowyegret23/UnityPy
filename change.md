# 변경사항 정리 (기준 커밋: `2c1156115c1ad635c2211a743029e5bd1804c76d` 이후 ~ 현재 `HEAD`)

## 대상 커밋 (총 4개)
1. `1964d74881da3a766530e5a784f69c3241f42293` - Reduce UnityFS memory spikes by removing large temporary copies
2. `049a6fed9601c60075e1ff1099d14151c2712b10` - Reduce save memory usage with streaming writes and save_to()
3. `5912f4cb5984061737ea667a98274e4902cf55b6` - file based
4. `5365b7a4f47bce809b28c098104528162ab51b15` - fix endian

## 변경 포인트 1: `File.read_files`에서 노드 데이터 복사 최소화
- 파일: `UnityPy/files/File.py`
- 과거 코드:
```python
for node in files:
    reader.Position = node.offset
    name = node.path
    node_reader = EndianBinaryReader(reader.read(node.size), offset=(reader.BaseOffset + node.offset))
```
- 현재 코드:
```python
is_memory_reader = hasattr(reader, "view")
base_offset = reader.BaseOffset

for node in files:
    name = node.path
    node_offset = node.offset

    if is_memory_reader:
        node_data = reader.view[node_offset : node_offset + node.size]
        node_reader = EndianBinaryReader(node_data, offset=(base_offset + node_offset))
    else:
        reader.Position = node_offset
        node_reader = EndianBinaryReader(reader.read(node.size), offset=(base_offset + node_offset))
```
- 변경 이유:
  - 메모리 기반 reader(`view` 보유)일 때 `reader.read()`로 노드별 대형 bytes 복사본을 만들지 않기 위함.
  - 대형 번들 로딩 시 피크 메모리 사용량(복사 스파이크) 감소.

## 변경 포인트 2: `CompressionHelper`에 iterable/파일 기반 압축 경로 추가
- 파일: `UnityPy/helpers/CompressionHelper.py`
- 과거 코드:
```python
file_data, block_info = chunk_based_compress(file_data, block_info_flag)
```
  - `chunk_based_compress(data: ByteString, ...)`만 사용 가능 (입력 전체를 한 번에 메모리에 보유).
- 현재 코드:
```python
def chunk_based_compress_iter(chunks: Iterable[ByteString], block_info_flag: int) -> Tuple[ByteString, list]:
    ...

def chunk_based_compress_iter_to_file(
    chunks: Iterable[ByteString], block_info_flag: int, out_path: str
) -> list:
    ...
```
  - `__all__`에 `chunk_based_compress_iter`, `chunk_based_compress_iter_to_file` 추가.
- 변경 이유:
  - 번들 payload를 "한 덩어리 bytes"로 만들지 않고 청크 스트림으로 압축하기 위함.
  - 파일 기반 저장 시 압축 결과까지 디스크로 직접 내보내 메모리 점유를 추가로 줄이기 위함.

## 변경 포인트 3: `EndianBinaryWriter`에 스트림 복사 API 추가
- 파일: `UnityPy/streams/EndianBinaryWriter.py`
- 과거 코드:
```python
writer.write_bytes(meta_writer.bytes)
writer.write_bytes(data_writer.bytes)
```
- 현재 코드:
```python
def write_stream(self, source: IOBase, chunk_size: int = 1048576):
    source.seek(0)
    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            break
        self.write(chunk)
```
- 변경 이유:
  - `source_writer.bytes` 접근 시 전체 bytes materialize(추가 복사)되는 문제를 피하기 위함.
  - 대용량 데이터 직렬화 시 스트리밍 복사로 메모리 사용량 완화.

## 변경 포인트 4: `SerializedFile` 저장 경로를 메모리/파일 기반으로 분리
- 파일: `UnityPy/files/SerializedFile.py`
- 과거 코드:
```python
def save(self, packer: Optional[str] = None) -> bytes:
    meta_writer = EndianBinaryWriter(endian=header.endian)
    data_writer = EndianBinaryWriter(endian=header.endian)
    ...
    writer.write_bytes(meta_writer.bytes)
    writer.align_stream(16)
    writer.write_bytes(data_writer.bytes)
    return writer.bytes
```
- 현재 코드:
```python
def _build_meta_and_data(self, data_writer: Optional[EndianBinaryWriter] = None):
    ...
    return meta_writer, data_writer

def _assemble(self, writer, meta_writer, data_writer):
    ...
    writer.write_stream(meta_writer.stream)
    writer.align_stream(16)
    writer.write_stream(data_writer.stream)

def save(self, packer: Optional[str] = None) -> bytes:
    meta_writer, data_writer = self._build_meta_and_data()
    writer = EndianBinaryWriter()
    self._assemble(writer, meta_writer, data_writer)
    return writer.bytes

def save_to(self, path: str, packer: Optional[str] = None) -> int:
    # data writer를 tempfile 기반으로 생성하여 디스크 경유 저장
    ...
```
- 변경 이유:
  - 기존 `save()` 로직의 대형 중간 버퍼 중복을 줄이기 위해 빌드/조립 단계를 분리.
  - `save_to(path)` 추가로 대용량 SerializedFile을 파일로 직접 저장 가능하게 하여 RAM 부담 완화.
  - `write_stream()` 사용으로 meta/data 버퍼를 bytes로 다시 만들지 않도록 개선.

## 변경 포인트 5: `BundleFile` 저장 경로를 스트리밍/파일 기반으로 확장
- 파일: `UnityPy/files/BundleFile.py`
- 과거 코드:
```python
data_writer = EndianBinaryWriter()
files = [
    (
        name,
        f.flags,
        data_writer.write_bytes(
            f.bytes if isinstance(f, (EndianBinaryReader, EndianBinaryWriter)) else f.save()
        ),
    )
    for name, f in self.files.items()
]
file_data = data_writer.bytes
...
file_data, block_info = CompressionHelper.chunk_based_compress(file_data, block_info_flag)
```
- 현재 코드:
```python
def save_to(self, path, packer=None):
    with open(path, "wb") as f:
        writer = EndianBinaryWriter(f)
        ...
    return os.path.getsize(path)

def save_fs(self, writer, data_flag, block_info_flag):
    is_file_backed = not isinstance(writer.stream, io.BytesIO)

    def iter_file_data():
        for name, f in self.files.items():
            if isinstance(f, (EndianBinaryReader, EndianBinaryWriter)):
                file_data = f.bytes
                yield file_data
            elif is_file_backed and hasattr(f, "save_to"):
                # SerializedFile은 임시파일 경유 스트리밍
                ...
                with open(tmp_path, "rb") as tmp_in:
                    while True:
                        chunk = tmp_in.read(1048576)
                        if not chunk:
                            break
                        yield chunk
            else:
                file_data = f.save()
                yield file_data

    if is_file_backed:
        block_info = CompressionHelper.chunk_based_compress_iter_to_file(...)
    else:
        file_data, block_info = CompressionHelper.chunk_based_compress_iter(...)
```
- 변경 이유:
  - `save()`만으로는 마지막에 전체 bundle bytes를 메모리에 들고 있어야 하므로 대용량에서 불리함.
  - `save_to()` + file-backed `save_fs()` 경로로 전환해 압축 데이터까지 temp 파일 경유 가능.
  - 기존 포맷 플래그/블록 정보 생성 로직은 유지하면서 메모리 피크를 낮추는 목적.

## 변경 포인트 6: `SerializedFile.save_to` 엔디안 수정
- 파일: `UnityPy/files/SerializedFile.py`
- 과거 코드:
```python
with open(path, "wb") as out:
    writer = EndianBinaryWriter(out, endian=self.header.endian)
    self._assemble(writer, meta_writer, data_writer)
```
- 현재 코드:
```python
with open(path, "wb") as out:
    # top-level header는 save()와 동일하게 big-endian으로 기록
    writer = EndianBinaryWriter(out)
    self._assemble(writer, meta_writer, data_writer)
```
- 변경 이유:
  - `save_to()`가 `save()`와 동일한 top-level header endian 동작을 하도록 맞춤.
  - 파일 기반 저장 도입 후 발생한 엔디안 불일치 위험 수정.

## 재적용 체크리스트 (sync fork 이후)
1. `UnityPy/helpers/CompressionHelper.py`
   - `chunk_based_compress_iter`, `chunk_based_compress_iter_to_file`, `__all__` 반영 확인
2. `UnityPy/streams/EndianBinaryWriter.py`
   - `write_stream` 반영 확인
3. `UnityPy/files/SerializedFile.py`
   - `_build_meta_and_data`, `_assemble`, `save_to`, `save`의 `write_stream` 반영 확인
   - `save_to`의 `writer = EndianBinaryWriter(out)` (엔디안 수정) 확인
4. `UnityPy/files/BundleFile.py`
   - `save_to` 추가
   - `save_fs`의 `iter_file_data` + file-backed 분기 + temp 파일 압축 경로 반영 확인
5. `UnityPy/files/File.py`
   - memory reader 분기(`reader.view` slice) 반영 확인

## 현재 워킹트리 추가 변경사항 (미커밋)

### 변경 포인트 7: UnityFS 블록 전체를 메모리에 합치지 않고 temp file + `mmap`으로 유지
- 파일: `UnityPy/files/BundleFile.py`
- 과거 코드:
```python
blocksReader = EndianBinaryReader(
    b"".join(
        self.decompress_data(
            reader.read_bytes(blockInfo.compressedSize),
            blockInfo.uncompressedSize,
            blockInfo.flags,
            i,
        )
        for i, blockInfo in enumerate(m_BlocksInfo)
    ),
    offset=(blocksInfoReader.real_offset()),
)
```
- 현재 코드:
```python
blocksReader = self._create_temp_backed_blocks_reader(
    reader,
    m_BlocksInfo,
    offset=(blocksInfoReader.real_offset()),
)
```
```python
def _create_temp_backed_blocks_reader(...):
    tmp_fd, tmp_path = tempfile.mkstemp(...)
    ...
    mmap_obj = mmap.mmap(tmp_file.fileno(), 0, access=mmap.ACCESS_READ)
    self._blocks_tmp_path = tmp_path
    self._blocks_tmp_file = tmp_file
    self._blocks_mmap = mmap_obj
    return EndianBinaryReader(memoryview(mmap_obj), offset=offset)
```
- 변경 이유:
  - `b"".join(...)`로 모든 decompressed block을 한 번에 메모리에 합치는 비용을 제거하기 위함.
  - 대형 UnityFS 번들을 열 때 RAM 피크를 줄이고, 원본 블록 데이터를 이후 저장 경로에서 재사용하기 위함.

### 변경 포인트 8: 번들 저장 시 `Replacer` 계층으로 원본 슬라이스/임시파일/bytes를 통합 처리
- 파일: `UnityPy/files/BundleFile.py`, `UnityPy/files/replacers.py`
- 과거 코드:
```python
for name, f in self.files.items():
    if isinstance(f, (EndianBinaryReader, EndianBinaryWriter)):
        file_data = f.bytes
    elif is_file_backed and hasattr(f, "save_to"):
        ...
    else:
        file_data = f.save()
    files.append((name, f.flags, len(file_data)))
    yield file_data
```
- 현재 코드:
```python
def build_replacer(name: str, f) -> Replacer:
    original_info = self._directory_info_map.get(name)
    if original_info is not None and self._blocks_reader is not None and not getattr(f, "is_changed", False):
        return SourceSliceReplacer.from_reader(
            self._blocks_reader,
            int(original_info.offset),
            int(original_info.size),
        )
    if isinstance(f, EndianBinaryReader):
        return SourceSliceReplacer.from_reader(f, 0, f.Length)
    if isinstance(f, EndianBinaryWriter):
        return SourceSliceReplacer.from_writer(f)
    if is_file_backed and hasattr(f, "save_to"):
        return TempFileReplacer(...)
    return BytesReplacer(f.save())
```
```python
def iter_file_data():
    for _name, _flags, replacer in file_entries:
        yield from replacer.iter_chunks()
```
- 변경 이유:
  - 변경되지 않은 bundle entry는 다시 `save()`하지 않고 원본 decompressed block slice를 그대로 재사용하기 위함.
  - 메모리/파일/temp source를 동일 인터페이스(`Replacer`)로 처리해 저장 경로를 단순화하기 위함.
  - file-backed save에서 대용량 entry를 디스크 기반으로 흘려보내기 위함.

### 변경 포인트 9: `ObjectReader` 대형 raw data를 spill store로 내리고 `Replacer`를 직접 저장
- 파일: `UnityPy/files/ObjectReader.py`
- 과거 코드:
```python
data: Optional[bytes] = None

...
if not writer:
    writer = EndianBinaryWriter(endian=self.reader.endian)
TypeTreeHelper.write_typetree(tree, node, writer, self.assets_file)
data = writer.bytes
self.set_raw_data(data)
return data
```
- 현재 코드:
```python
data: Optional[Union[bytes, bytearray, memoryview, Replacer]] = None
_SPILL_RAW_DATA_THRESHOLD = 8 * 1024 * 1024
```
```python
spill_to_file = owns_writer and int(self.byte_size or 0) >= _SPILL_RAW_DATA_THRESHOLD
if spill_to_file:
    spill_store = self.assets_file.get_spill_store()
    writer, spill_segment = spill_store.create_writer(endian=self.reader.endian)
...
spilled = self.assets_file.get_spill_store().slice(
    spill_segment.start,
    spill_segment.length,
)
self.set_raw_data(spilled)
return spilled
```
```python
if isinstance(data, Replacer):
    data.write_to(data_writer)
else:
    data_writer.write(data)
```
- 변경 이유:
  - 큰 typetree 저장 결과를 항상 `writer.bytes`로 메모리에 올리면 객체 단위로 대형 복사가 발생하기 때문.
  - 8 MiB 이상 payload는 spill file에 저장하고 slice handle만 들고 있게 해 RAM 사용량을 줄이기 위함.
  - `ObjectReader.write()`가 `Replacer`를 직접 흘려보낼 수 있게 하여 재직렬화 중 추가 복사를 줄이기 위함.

### 변경 포인트 10: `SerializedFile`이 spill store lifecycle을 관리
- 파일: `UnityPy/files/SerializedFile.py`
- 과거 코드:
```python
self._cache = {}
self.unknown = 0
```
- 현재 코드:
```python
self._cache = {}
self.unknown = 0
self._spill_store = None

def get_spill_store(self) -> AppendOnlySpillStore:
    if self._spill_store is None:
        self._spill_store = AppendOnlySpillStore(
            prefix=f"unitypy_{self.name or 'asset'}_spill_",
        )
    return self._spill_store

def close(self) -> None:
    if self._spill_store is not None:
        self._spill_store.close()
        self._spill_store = None

def __del__(self):
    self.close()
```
- 변경 이유:
  - `ObjectReader.save_typetree()`의 spill file을 asset 파일 단위로 재사용/정리하기 위함.
  - 임시 spill 파일이 누수되지 않도록 lifecycle을 `SerializedFile`에 묶기 위함.

### 변경 포인트 11: `replacers.py` 신규 추가
- 파일: `UnityPy/files/replacers.py`
- 추가 내용:
```python
class Replacer(Protocol): ...
class BytesReplacer: ...
class SourceSliceReplacer: ...
class TempFileReplacer: ...
class SpillStoreSliceReplacer: ...
class AppendOnlySpillStore: ...
```
- 변경 이유:
  - bytes / memoryview / reader slice / temp file / spill store slice를 공통 인터페이스로 다루기 위함.
  - bundle save와 object raw data 보관 경로가 같은 방식으로 chunk streaming 할 수 있게 만들기 위함.

### 변경 포인트 12: 회귀 방지 테스트 추가
- 파일:
  - `tests/test_bundle_memory_helpers.py`
  - `tests/test_objectreader_spill.py`
  - `tests/test_replacers.py`
- 추가 내용:
  - `BundleFile._create_temp_backed_blocks_reader()`가 temp storage를 사용하고 cleanup 되는지 검증
  - `ObjectReader.save_typetree()`가 큰 payload에서 spill store를 사용하고 `write()`/`get_raw_data()`와 연동되는지 검증
  - `BytesReplacer`, `SourceSliceReplacer`, `TempFileReplacer`, `AppendOnlySpillStore`의 기본 동작 검증
- 변경 이유:
  - 이번 변경은 "기능 추가"보다 "메모리 사용 방식 변경" 성격이 강해서, 회귀가 나면 발견이 늦어질 수 있음.
  - 스트리밍/임시파일/cleanup 경로가 실제로 동작하는지 자동 테스트로 고정하기 위함.
