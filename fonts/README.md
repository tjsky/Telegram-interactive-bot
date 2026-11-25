# 验证码字体

四个字体，全部 **SIL Open Font License 1.1**，可随本项目一起分发和商用。
每个字体的完整许可证在同目录 `LICENSE-*.txt`。

| 文件 | 字体 | 风格 | 来源 |
|---|---|---|---|
| `Inter-Bold.otf` | Inter Bold | 几何无衬线，字形最规整 | [rsms/inter](https://github.com/rsms/inter) |
| `JetBrainsMono-Bold.ttf` | JetBrains Mono Bold | 等宽，字面宽、字距固定 | [JetBrains/JetBrainsMono](https://github.com/JetBrains/JetBrainsMono) |
| `ComicNeue-Bold.ttf` | Comic Neue Bold | 手写圆润，笔画对比弱 | [crozynski/comicneue](https://github.com/crozynski/comicneue) |
| `ZillaSlab-Bold.ttf` | Zilla Slab Bold | 板衬线，末端有衬线突起 | [mozilla/Zilla-Slab](https://github.com/mozilla/Zilla-Slab) |

## 为什么是这四个

- **风格必须拉开**：同一张图里每个字符随机取字体，四种差异明显的字形能让
  OCR 的字符分类器失效；风格太接近等于只有一个字体。
- **不能用极端字形**：实测过 Archivo Black（超粗，笔画糊成一团）和
  Bebas Neue（超窄、笔画偏细），都是先伤人类可读性、后伤 OCR，已排除。
- **不用系统字体**：`C:/Windows/Fonts` 里的 arial / comic / trebuc 是微软字体，
  随项目分发有版权问题，且不同 Windows 版本字形不一致。程序只在
  `fonts/` 为空时才回退到 Linux 的 DejaVu / Liberation（同为自由字体）。

## 替换字体

把新字体（建议粗体，且是拉丁字体）放进本目录，然后改
`interactive-bot/__main__.py` 里的 `_BUNDLED_FONTS` 元组。
中日韩字体的拉丁字形比例怪异，混进来会让验证码忽胖忽瘦，不要放。

改完跑一次渲染确认可读性：

```bash
python -m interactive-bot.captcha_engine   # 生成 preview/ 网格图
```
