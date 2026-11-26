# TSM300-tympanometer

Oscilla TSM-300 임피던스 청력검사기(tympanometer)의 검사 결과를 검사 직후 PC로 가져오는 프로그램입니다.

장비에서 검사를 마치고 `->` 키를 누르면, PC가 결과를 받아 수치와 그래프 이미지로 저장합니다.

## 설치 (Windows, Python 3.9 이상)

```bash
python -m pip install --upgrade https://github.com/anveer2-cyber/TSM300-tympanometer/archive/refs/heads/claude/cool-hamilton-bdp178.zip
```

`python` 명령이 없다고 나오면 `py -m pip ...`, `py -m tsm300` 으로 실행합니다.

## 장비 쪽 준비

- `MENU` -> `Right key` -> `TRANSMIT`
- `MENU` -> `Printer setup` -> `Printer Port` -> `Parallel`
- 장비 뒷면 `SERIAL` 단자와 PC를 케이블로 연결

## 결과 받기

```bash
python -m tsm300 --show
```

1. 포트를 찾아 연결합니다. 여러 개면 번호로 고릅니다. `--port COM5` 로 바로 지정할 수 있습니다.
2. 장비에서 검사 결과가 화면에 떠 있는 상태로 `->` 키를 누릅니다.
3. PC 화면에 수치가 표시되고 `results` 폴더에 파일 세 개가 저장됩니다.
4. `Ctrl+C` 로 끝냅니다.

`--show` 를 붙이면 결과 이미지를 받자마자 화면에 엽니다. `--out 폴더` 로 저장 위치를 바꿀 수 있습니다. 케이블이 빠지거나 포트가 끊기면 5초마다 다시 연결을 시도합니다.

### 저장되는 파일

검사 한 번마다 받은 시각을 이름으로 한 파일 세 개가 생깁니다.

| 파일 | 내용 |
| --- | --- |
| `20261126_211205.png` | 양쪽 귀의 고막운동도(정상 범위 사각형 포함), 수치, 등골근 반사 그래프 |
| `20261126_211205.json` | 수치와 곡선 데이터 (다른 프로그램 연동용) |
| `20261126_211205.bin` | 장비가 보낸 원본 데이터 |

장비가 보내는 것은 장비 메모리의 ID 번호(1~20)뿐이고 환자 이름은 없습니다. 환자 정보는 이후 예진이텔레그램 연동으로 붙일 예정입니다.

## 그 밖의 명령

| 명령 | 하는 일 |
| --- | --- |
| `python -m tsm300 ports` | 시리얼 포트 목록 |
| `python -m tsm300 decode 파일.bin` | 저장해 둔 원본 데이터를 다시 해독해 이미지와 JSON 생성 |
| `python -m tsm300 capture` | 장비가 보내는 바이트를 해독 없이 그대로 기록 (형식 분석용) |
| `python -m tsm300 capture --scan` | 통신 속도를 모르는 장비에서 속도부터 찾기 |

## 다음 단계

- 예진이텔레그램 검사실 정보에서 환자 확인
- 결과를 상쾌한캡처로 전달
- PC 시작 시 자동 실행

장비와 통신 형식에 대해 알아낸 내용은 [docs/device-notes.md](docs/device-notes.md) 에 있습니다.

## 개발

```bash
python -m pip install -e ".[dev]"
```

```bash
python -m pytest
```

`tests/data` 의 파일은 실제 장비에서 받은 전송입니다.
