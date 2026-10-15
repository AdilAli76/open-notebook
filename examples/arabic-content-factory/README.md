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

## رادار الترند: `radar.py`

يبحث في أحدث فيديوهات يوتيوب للمواضيع التي تحددها، ويضيف روابط يوتيوب الرائجة في subreddits تختارها، ثم:
- يرتّبها بحسب **المشاهدات في الساعة** و**التفاعل** (الإعجابات والتعليقات) وشعبيتها على Reddit.
- يستبعد ما له **صوت عربي** مسبقًا أو **نسخ عربية** على يوتيوب، والبث المباشر، والمقاطع القصيرة جدًا أو الطويلة جدًا.
- يصنّف كل فيديو: **✅ CC** (ترخيص Creative Commons، يمكن دبلجته ونشره مع ذكر المصدر) أو **📩 تواصل** (عميل محتمل: اعرض على القناة دبلجة محتواها).
- لا يكرر الفيديوهات نفسها في التشغيلات التالية (`output/radar_seen.json`).

```bash
python radar.py --topic "AI agents" --topic "personal finance" \
  --subreddit artificial --subreddit technology --top 10

# تلقائي بالكامل: دبلجة أفضل 3 فيديوهات CC باللهجة المصرية
python radar.py --topic "AI news" --auto-dub 3 --dub-args "--dialect egyptian"
```

الناتج: تقرير عربي `output/radar_<التاريخ>.md` وملف بيانات `.json`.

**تشغيل يومي آلي** (cron، كل يوم الساعة 7 صباحًا):
```cron
0 7 * * * cd /path/to/arabic-content-factory && python3 radar.py --topic "AI news" --auto-dub 3 >> radar.log 2>&1
```

> ⚖️ الدبلجة التلقائية لا تشمل إلا فيديوهات Creative Commons. أي فيديو آخر لا تُعِد نشره قبل أخذ إذن صاحبه، وهو في الوقت نفسه فرصتك لبيع خدمة الدبلجة له.

## خارطة الطريق: من سكربت إلى منتج

1. ✅ **دبلجة كاملة مع مزامنة الشفاه**: `dub.py`
2. ✅ **رادار الترند**: `radar.py`
3. **مقاطع قصيرة تلقائية**: قص أفضل اللحظات مع ترجمة عربية محروقة على الفيديو (ffmpeg + WhisperX) وصورة مصغّرة مولَّدة (ComfyUI/Flux).
4. **نشر تلقائي**: Postiz لجدولة النشر على تيك توك ويوتيوب شورتس وإنستغرام، وn8n لتنسيق السير كله.
5. **بوابة عملاء**: يرفع العميل رابطًا ويختار اللهجة والصوت ويدفع، ثم يستلم فيديو مدبلجًا.

> ⚖️ VoiceStudio مرخّص بـ AGPL-3.0؛ بيع المخرجات مسموح، لكن إذا أتحت نسخة معدّلة منه كخدمة على الإنترنت يلزمك نشر تعديلاتك. لا تُعِد نشر محتوى الآخرين دون إذن أصحابه.
