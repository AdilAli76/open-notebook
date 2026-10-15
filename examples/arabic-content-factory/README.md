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

## مقاطع قصيرة ونشر تلقائي: `publish.py`

يأخذ مجلد الناتج من `dub.py` ثم:
1. **يختار أقوى اللحظات** (20 إلى 60 ثانية). يختارها Open Notebook بنموذج لغوي، ويقترح لكل مقطع عنوانًا ووصفًا وهاشتاغات. إن لم يتوفر النموذج يستخدم اختيارًا محليًا بحسب كثافة الكلام.
2. **يحوّل كل مقطع إلى فيديو عمودي** بمقاس 1080×1920، بخلفية مموّهة وترجمة عربية مكتوبة على الفيديو.
3. **يرسلها إلى [Postiz](https://github.com/gitroomhq/postiz-app)**، وهو مفتوح المصدر ويمكن تشغيله على سيرفرك. ينشئ منشورًا لكل قناة: يوتيوب شورتس، تيك توك، إنستغرام ريلز. يُذكر المصدر تلقائيًا في كل منشور، ويُفعَّل إفصاح «صُنع بالذكاء الاصطناعي» على تيك توك.

```bash
export POSTIZ_API_KEY=...                                   # Postiz ← Settings ← Public API
export POSTIZ_API=https://your-postiz.example.com/api/public/v1   # للنسخة المستضافة ذاتيًا

# مسودات للمراجعة (الوضع الافتراضي والأكثر أمانًا)
python publish.py output/dub_20261015_120000

# جدولة 3 مقاطع، واحد كل 6 ساعات بدءًا من 9 صباحًا
python publish.py output/dub_... --schedule --start 2026-10-16T09:00:00+03:00

# تجهيز المقاطع فقط بلا نشر
python publish.py output/dub_... --no-publish
```

| الخيار | الوصف |
|---|---|
| `--clips` · `--min-seconds` · `--max-seconds` | عدد المقاطع وطولها |
| `--picker` | `llm` (افتراضي) · `auto` |
| `--channels` | معرّفات قنوات Postiz، مثل `youtube,tiktok,instagram` |
| `--hashtags` | هاشتاغات ثابتة تُضاف لكل منشور |
| `--credit` | سطر ذكر المصدر (افتراضيًا رابط الفيديو الأصلي) |
| `--font` | خط الترجمة (ثبّت `fonts-noto-core` للحصول على Noto Sans Arabic) |

الناتج: `shorts/short_XX.mp4` و`shorts/clips.json`.

## السلسلة الكاملة يوميًا

```cron
0 7 * * * cd /path/to/arabic-content-factory && python3 radar.py --topic "AI news" --auto-dub 2 --dub-args "--dialect egyptian" && for d in output/dub_*/; do [ -d "$d/shorts" ] || python3 publish.py "$d"; done >> factory.log 2>&1
```

## بوابة العملاء: `portal/`

موقع عربي يبيع خدمة الدبلجة ويشغّلها تلقائيًا:

1. العميل **يلصق رابطًا أو يرفع ملفًا**، ويختار اللهجة والإضافات: مزامنة الشفاه، وحزمة 3 مقاطع قصيرة.
2. يُحسب **السعر فورًا** من مدة الفيديو: رسوم ثابتة + سعر للدقيقة + سعر الإضافات.
3. **الدفع** بإحدى طريقتين:
   - `manual`: تحويل بنكي أو محفظة، ثم تؤكد الدفع من لوحة التحكم. يعمل في أي دولة.
   - `stripe`: دفع بالبطاقة، وتبدأ الدبلجة تلقائيًا بعد وصول إشعار الدفع الموقَّع.
4. **عامل خلفي** يشغّل `dub.py`، ثم `publish.py --no-publish` للمقاطع القصيرة، طلبًا واحدًا في كل مرة حتى لا يُحمَّل كرت الشاشة فوق طاقته.
5. يتابع العميل طلبه من **رابط سري خاص** ويحمّل ملفاته منه. الملفات تُحذف تلقائيًا بعد 30 يومًا.
6. **لوحة تحكم** `/admin`: الطلبات، والإيراد، وتأكيد الدفع، وإعادة المحاولة، والإلغاء.

```bash
cd portal && pip install -r requirements.txt
export PORTAL_ADMIN_PASSWORD='كلمة-سر-قوية'
export PORTAL_PUBLIC_URL=https://dub.example.com
export PAYMENT_INSTRUCTIONS='حوّل إلى IBAN: SA00... أو محفظة: 05...'
uvicorn app:app --host 0.0.0.0 --port 8080
```

| المتغير | الافتراضي | الوصف |
|---|---|---|
| `PORTAL_BRAND` | دبلجة عربية بالذكاء الاصطناعي | اسم الموقع |
| `PRICE_CURRENCY` · `PRICE_BASE` · `PRICE_PER_MIN` | `usd` · 3 · 1.5 | التسعير |
| `PRICE_LIPSYNC_PER_MIN` · `PRICE_SHORTS` | 2 · 5 | سعر الإضافات |
| `MAX_MINUTES` · `MAX_UPLOAD_MB` | 30 · 500 | حدود الطلب |
| `PAYMENT_PROVIDER` | `manual` | `manual` أو `stripe` |
| `STRIPE_SECRET_KEY` · `STRIPE_WEBHOOK_SECRET` | — | لـ Stripe. اجعل عنوان الـ webhook `/stripe/webhook` للحدث `checkout.session.completed` |
| `DUB_EXTRA_ARGS` | — | خيارات إضافية تمرَّر إلى `dub.py`، مثل `--voicestudio-api ...` |
| `PORTAL_DATA` · `RETENTION_DAYS` | `portal/data` · 30 | مكان التخزين ومدة الاحتفاظ بالملفات |

> 🔒 في الإنتاج شغّل البوابة خلف HTTPS، مثلًا Caddy أو Nginx، فالدفع ولوحة التحكم يحتاجان اتصالًا مشفّرًا. يجب أن يؤكد العميل امتلاكه حقوق الفيديو قبل الطلب.

## خارطة الطريق: من سكربت إلى منتج

1. ✅ **دبلجة كاملة مع مزامنة الشفاه**: `dub.py`
2. ✅ **رادار الترند**: `radar.py`
3. ✅ **مقاطع قصيرة تلقائية** (`publish.py`)، وبقي منها: صورة مصغّرة مولَّدة (ComfyUI/Flux).
4. ✅ **نشر تلقائي عبر Postiz** (`publish.py`)، وبقي منه: لوحة تنسيق مرئية (n8n) بدل cron.
5. ✅ **بوابة عملاء** (`portal/`): يرفع العميل رابطًا ويختار اللهجة والصوت ويدفع، ثم يستلم فيديو مدبلجًا.

> ⚖️ VoiceStudio مرخّص بـ AGPL-3.0؛ بيع المخرجات مسموح، لكن إذا أتحت نسخة معدّلة منه كخدمة على الإنترنت يلزمك نشر تعديلاتك. لا تُعِد نشر محتوى الآخرين دون إذن أصحابه.
