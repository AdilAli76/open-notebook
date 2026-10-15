# Arabic Content Factory — مصنع المحتوى العربي

يحوّل أي فيديو يوتيوب إنجليزي إلى **سكربت عربي باللهجة التي تختارها + تعليق صوتي عربي** باستخدام خدمات مفتوحة المصدر تعمل على سيرفرك:

| المرحلة | الخدمة | الدور |
|---|---|---|
| 1 | Agent-Reach (`yt-dlp`) | جلب العنوان ونص الفيديو |
| 2 | Open Notebook API (`:5055`) | كتابة السكربت العربي بأي نموذج لغوي مضبوط عندك |
| 3 | VoiceStudio API (`:3900`) | توليد الصوت العربي (صوت افتراضي أو صوت مستنسخ) |

## التشغيل

```bash
# المتطلبات: yt-dlp و ffmpeg، وخدمتا Open Notebook و VoiceStudio تعملان
agent-reach install --env=auto        # يثبّت yt-dlp ويضبطه
export OPEN_NOTEBOOK_PASSWORD=...     # إن كانت كلمة مرور الـ API مفعّلة

python factory.py --url "https://www.youtube.com/watch?v=XXXX" \
  --style summary --dialect egyptian --voice <voice_profile_id>
```

| الخيار | القيم |
|---|---|
| `--style` | `summary` · `shorts` · `podcast` · `translate` |
| `--dialect` | `msa` · `egyptian` · `gulf` · `levantine` · `maghrebi` |
| `--voice` | معرّف بروفايل صوت من VoiceStudio (يمكن أن يكون صوتك المستنسخ) |
| `--text-file` | استخدام نص محلي بدل رابط يوتيوب |
| `--no-audio` | السكربت فقط |

الناتج في `output/<العنوان>/`: الملفات `transcript.txt` و`script_ar.txt` و`audio_ar.mp3`.

## الدبلجة الكاملة مع مزامنة الشفاه: `dub.py`

يأخذ فيديو (رابطًا أو ملفًا) ويُخرج نسخة عربية مدبلجة، يحتفظ فيها كل متكلم بنبرة صوته الأصلية:

| # | المرحلة | الأداة |
|---|---|---|
| 1 | تنزيل الفيديو وفصل الصوت عن الموسيقى | VoiceStudio (Demucs) |
| 2 | تفريغ النص وتمييز المتكلمين | VoiceStudio (WhisperX + pyannote) |
| 3 | ترجمة إلى العربية باللهجة المختارة | VoiceStudio (مترجم LLM) |
| 4 | توليد الصوت العربي باستنساخ صوت كل متكلم | VoiceStudio |
| 5 | تصدير الفيديو مع الموسيقى الأصلية | VoiceStudio |
| 6 | تحريك الشفاه لتطابق الكلام العربي (اختياري) | [LatentSync](https://github.com/bytedance/LatentSync) |

```bash
# دبلجة فقط
python dub.py --url "https://www.youtube.com/watch?v=XXXX" --dialect gulf

# دبلجة + مزامنة الشفاه (تحتاج GPU)
git clone https://github.com/bytedance/LatentSync && (cd LatentSync && source setup_env.sh)
python dub.py --file talk.mp4 --dialect egyptian --lipsync --latentsync-dir ./LatentSync
```

| الخيار | الوصف |
|---|---|
| `--dialect` | `msa` · `egyptian` · `gulf` · `levantine` · `maghrebi` |
| `--quality` | `cinematic` (افتراضي، مطلوب للهجات) · `fast` |
| `--voice` | فرض صوت واحد لكل المتكلمين بدل استنساخ أصواتهم |
| `--speakers` | عدد المتكلمين إن كان معروفًا (يحسّن الفصل بينهم) |
| `--timing` | `smart_fit` (افتراضي) · `concise` · `strict_slot` · `stretch_video` |
| `--lipsync-cmd` | أمر بديل لمزامنة الشفاه، مثل MuseTalk، مع `{video}` و`{audio}` و`{out}` |

الناتج في `output/dub_<التاريخ>/`: الملفات `dubbed_ar.mp4` و`dubbed_ar_lipsync.mp4` و`segments.json` (النص الأصلي والعربي لكل مقطع، مع المتكلم والتوقيت).

> لتعمل الترجمة باللهجات، اضبط مزوّد LLM في إعدادات VoiceStudio (Settings ← Translation).

## خارطة الطريق: من سكربت إلى منتج

1. ✅ **دبلجة كاملة مع مزامنة الشفاه**: `dub.py`
2. **رادار الترند**: Agent-Reach يراقب يوتيوب وReddit وX ويرشّح تلقائيًا الأكثر انتشارًا مما لم يُترجَم للعربية بعد.
3. **مقاطع قصيرة تلقائية**: قص أفضل اللحظات مع ترجمة عربية محروقة على الفيديو (ffmpeg + WhisperX) وصورة مصغّرة مولَّدة (ComfyUI/Flux).
4. **نشر تلقائي**: Postiz لجدولة النشر على تيك توك ويوتيوب شورتس وإنستغرام، وn8n لتنسيق السير كله.
5. **بوابة عملاء**: يرفع العميل رابطًا ويختار اللهجة والصوت ويدفع، ثم يستلم فيديو مدبلجًا.

> ⚖️ VoiceStudio مرخّص بـ AGPL-3.0؛ بيع المخرجات مسموح، لكن إذا أتحت نسخة معدّلة منه كخدمة على الإنترنت يلزمك نشر تعديلاتك. لا تُعِد نشر محتوى الآخرين دون إذن أصحابه.
