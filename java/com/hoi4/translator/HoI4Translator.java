package com.hoi4.translator;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Java-сервис перевода для HoI4.
 * Вызывается из Python через JPype.
 * Java 11+ совместим.
 */
public class HoI4Translator {

    // Маркеры, которые Python подставляет вместо $ключ$, [ключ], §X и т.д.
    private static final Pattern PH_PATTERN = Pattern.compile("<<PH\\d+>>");

    // FIX #10: HttpClient создаётся один раз, а не на каждый вызов
    private final java.net.http.HttpClient httpClient = java.net.http.HttpClient.newBuilder()
            .connectTimeout(java.time.Duration.ofSeconds(8))
            .followRedirects(java.net.http.HttpClient.Redirect.NORMAL)
            .build();

    // FIX #9: надёжный regex для первого сегмента ответа Google вместо хрупкого indexOf+split
    // Ответ: [[["Перевод","Оригинал",...],...],...] — берём только первое поле первого элемента
    private static final Pattern GOOGLE_SEG_RE = Pattern.compile(
            "\\[\"((?:[^\"\\\\]|\\\\.)*)\",\"(?:[^\"\\\\]|\\\\.)*\"");

    // FIX #11: лимит длины текста для одного запроса (символов в исходном тексте)
    private static final int MAX_QUERY_CHARS = 400;

    /**
     * Основной метод, вызываемый из Python.
     * @param text строка с защищёнными плейсхолдерами <<PH0>>, <<PH1>>...
     * @param targetLang код целевого языка (ru, de, pl, etc.)
     * @return переведённая строка с сохранёнными маркерами
     */
    public String translate(String text, String targetLang) {
        if (text == null || text.isEmpty()) return text;

        List<String> segments = splitPreservingPlaceholders(text);
        StringBuilder result = new StringBuilder(text.length());

        for (String segment : segments) {
            if (PH_PATTERN.matcher(segment).matches()) {
                // Плейсхолдер — не трогаем, иначе Python не восстановит $ключ$
                result.append(segment);
            } else {
                // Переводим только "чистый" текст
                result.append(translateSegment(segment, targetLang));
            }
        }
        return result.toString();
    }

    /** Разбивает текст на плейсхолдеры и обычные фрагменты */
    private List<String> splitPreservingPlaceholders(String text) {
        List<String> parts = new ArrayList<>();
        Matcher m = PH_PATTERN.matcher(text);
        int lastEnd = 0;
        while (m.find()) {
            if (m.start() > lastEnd) parts.add(text.substring(lastEnd, m.start()));
            parts.add(m.group());
            lastEnd = m.end();
        }
        if (lastEnd < text.length()) parts.add(text.substring(lastEnd));
        return parts;
    }

    /**
     * ЗДЕСЬ ЛОГИКА ПЕРЕВОДА.
     * Сейчас стоит безопасная заглушка. Ниже в комментариях — готовый код для бесплатного API.
     */
    private String translateSegment(String text, String targetLang) {
        String clean = text.trim();
        if (clean.isEmpty()) return text;

        // ==========================================
        // 🔵 GOOGLE TRANSLATE (неофициальный бесплатный эндпоинт)
        // ==========================================

        // FIX #11: обрезаем слишком длинные строки — длинный URL вернёт ошибку
        String query = clean.length() > MAX_QUERY_CHARS ? clean.substring(0, MAX_QUERY_CHARS) : clean;

        try {
            String encoded = java.net.URLEncoder.encode(query, java.nio.charset.StandardCharsets.UTF_8);
            String url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=" +
                    targetLang + "&dt=t&q=" + encoded;

            // FIX #10: используем общий httpClient, а не создаём новый на каждый вызов
            java.net.http.HttpRequest req = java.net.http.HttpRequest.newBuilder()
                    .uri(java.net.URI.create(url))
                    .header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                    .header("Accept", "application/json")
                    .GET()
                    .build();

            java.net.http.HttpResponse<String> resp = httpClient.send(req, java.net.http.HttpResponse.BodyHandlers.ofString());

            if (resp.statusCode() == 200) {
                String translated = parseGoogleResponse(resp.body());
                if (!translated.isEmpty()) return translated;
            }
        } catch (java.net.http.HttpTimeoutException e) {
            System.err.println("[Java] Google Translate: timeout");
        } catch (Exception e) {
            System.err.println("[Java] Google Translate error: " + e.getMessage());
        }

        // Фолбэк: возвращаем оригинал, чтобы не ломать пайплайн
        return text;
    }

    /** FIX #9: Парсит ответ Google API через regex — надёжнее чем indexOf+split.
     *  Собирает все сегменты перевода из data[0][*][0]. */
    private String parseGoogleResponse(String json) {
        StringBuilder sb = new StringBuilder();
        Matcher m = GOOGLE_SEG_RE.matcher(json);
        while (m.find()) {
            String seg = m.group(1);
            // Восстанавливаем JSON-экранирование
            seg = seg.replace("\\n", "\n")
                    .replace("\\t", "\t")
                    .replace("\\\"", "\"")
                    .replace("\\\\", "\\");
            sb.append(seg);
        }
        return sb.toString();
    }

    /** Тест из консоли */
    public static void main(String[] args) {
        HoI4Translator t = new HoI4Translator();
        String test = "We must defend <<PH0>> and secure §Y$PROVINCE$§!.";
        System.out.println("IN:  " + test);
        System.out.println("OUT: " + t.translate(test, "ru"));
    }
}
