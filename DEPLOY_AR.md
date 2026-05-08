# خطوات التشغيل السريعة

1. افتح `config.env`.
2. ضع عنوان XMR في `XMR_WALLET`.
3. ارفع المشروع على Heroku.
4. من Resources اجعل worker = 1 فقط.
5. من Logs انتظر ظهور `accepted`.

إذا ظهر كود -9:

- اجعل `RESOURCE_PERCENT="10"`
- اجعل `DUTY_WORK_SECONDS="3"`
- اجعل `DUTY_SLEEP_SECONDS="30"`
- أعد النشر.
