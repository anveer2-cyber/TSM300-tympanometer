# TSM300-tympanometer

Oscilla TSM-300 임피던스 청력검사기(tympanometer)의 검사 결과를 검사 직후 PC로 가져와, 환자 정보와 함께 A4 결과지(JPG)로 저장하는 프로그램입니다.

## 설치 (Windows, Python 3.9 이상)

```bash
python -m pip install --upgrade https://github.com/anveer2-cyber/TSM300-tympanometer/archive/refs/heads/claude/cool-hamilton-bdp178.zip
```

`python` 명령이 없다고 나오면 `py -m pip ...`, `py -m tsm300` 으로 실행합니다.

## 장비 쪽 준비

- `MENU` -> `Right key` -> `TRANSMIT`
- `MENU` -> `Printer setup` -> `Printer Port` -> `Parallel`
- 장비 뒷면 `SERIAL` 단자와 PC를 케이블로 연결

## 실행

```bash
python -m tsm300
```

창이 뜨고 USB-시리얼 포트에 자동으로 연결합니다. 포트를 직접 정하려면 `python -m tsm300 --port COM5` 로 실행합니다.

### 검사 순서

1. 왼쪽 **환자 정보**에 차트번호, 이름, 성별, 생년월일을 넣습니다. 위쪽에 "다음 검사는 ○○○ 환자로 저장됩니다"가 표시됩니다.
2. 검사를 마치고 장비에서 `->` 키를 누릅니다.
3. 결과지가 화면에 나타나고 `results` 폴더에 저장됩니다.

환자 정보를 넣지 않았거나 앞 환자 정보가 그대로 남아 있을 때는, 결과를 환자 정보 없이 저장해 두고 알려 줍니다. 환자 정보를 확인한 뒤 **이 환자 정보로 저장**을 누르면 결과지에 반영됩니다. 앞 환자의 이름이 다음 환자 결과에 잘못 붙지 않게 하려는 장치입니다.

### 저장되는 파일

검사 한 번마다 받은 시각과 차트번호를 이름으로 한 파일 세 개가 생깁니다. 파일 이름에 환자 이름은 넣지 않습니다.

| 파일 | 내용 |
| --- | --- |
| `20261126_211205_00512.jpg` | A4 결과지 (300 dpi, 2480×3508). 환자 정보, 양쪽 고막운동도, 측정값 표, 등골근 반사 |
| `20261126_211205_00512.json` | 환자 정보, 수치, 곡선 데이터 |
| `20261126_211205_00512.bin` | 장비가 보낸 원본 데이터 |

## 그 밖의 명령

| 명령 | 하는 일 |
| --- | --- |
| `python -m tsm300 listen` | 창 없이 명령 프롬프트에서 결과를 받아 저장 (환자 정보 없음) |
| `python -m tsm300 decode 파일.bin --chart 00512 --name 홍길동` | 저장해 둔 원본 데이터로 결과지를 다시 만들기 |
| `python -m tsm300 ports` | 시리얼 포트 목록 |
| `python -m tsm300 capture` | 장비가 보내는 바이트를 해독 없이 그대로 기록 (형식 분석용) |

장비와 통신 형식에 대해 알아낸 내용은 [docs/device-notes.md](docs/device-notes.md) 에 있습니다.

## 개발

```bash
python -m pip install -e ".[dev]"
```

```bash
python -m pytest
```

`tests/data` 의 파일은 실제 장비에서 받은 전송입니다. 화면 테스트는 `QT_QPA_PLATFORM=offscreen` 환경변수를 주면 화면 없이 돌아갑니다.
