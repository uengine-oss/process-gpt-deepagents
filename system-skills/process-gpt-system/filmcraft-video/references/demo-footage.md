# FilmCraft 데모 소재

사용자 소재가 없고 풍경 영상이면 되는 요청(샘플 영상, 배경 영상, 시험용 클립)에 쓴다.

```json
filmcraft_command_run {"id": "file.importDemoFootage", "params": {"scene": "OceanSunset"}}
```

| scene | 장면 |
|---|---|
| `OceanSunset` | 바다 위 해 질 녘 |
| `Aurora` | 오로라 |
| `CityNight` | 밤 도시 |
| `Dunes` | 사막 모래 언덕 |
| `Plasma` | 추상 플라스마 |
| `Forest` | 숲 |

1920×1080 클립이다 — `file.newSequenceFromClip` 으로 만든 시퀀스에 그대로 꽉 찬다.
데모 소재를 썼으면 답에 「데모 풍경 소재」라고 밝힌다.
