# assets/

## loopback_demo_2spk_40s.wav

兜底验证音频: 真实双人访谈(非合成), 40s, 16kHz S16 单声道, RMS 归一化(响度提升 2.8x)。

- 来源: NPR Fresh Air — Terry Gross 访谈 Gene Simmons(archive.org
  `TerryGrossInterviewWithGeneSimmons`), 截取 12:00–12:40 纯对话段:
  A(Terry) "Jean Simmons, welcome to Fresh Air" → 提问 → B(Gene) 回答
  "Close but no guitars. It's Chaim Witz" …
- 用途: `./run.sh --loopback assets/loopback_demo_2spk_40s.wav --language en`
  wav 直送流水线, 同时经 XVF3800 扬声器以最大音量播放, 不依赖麦克风拾音。
- 预期: 7 段左右、终态 2 个说话人; A 的介绍/提问同簇, B 的回答同簇
  (个别 <1.5s 碎片段可能误归, 属短段 embedding 噪声, 不影响结论)。
