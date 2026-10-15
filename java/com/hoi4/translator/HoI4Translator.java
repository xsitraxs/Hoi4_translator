package com.hoi4.translator;

import java.io.IOException;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Java service for HoI4 localization, called from Python through JPype.
 * Compatible with Java 11+.
 */
public class HoI4Translator {

    // The Python layer replaces Paradox tokens with markers before calling Java.
    private static final Pattern PH_PATTERN = Pattern.compile("<<PH\\d+>>");

    private static final Pattern GOOGLE_SEG_RE = Pattern.compile(
            "\\[\"((?:[^\"\\\\]|\\\\.)*)\",\"(?:[^\"\\\\]|\\\\.)*\"");

    // Keep requests small enough for the unofficial Google endpoint, but never truncate text.
    private static final int MAX_QUERY_CHARS = 400;
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(20);

    private final HttpClient httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(8))
            .followRedirects(HttpClient.Redirect.NORMAL)
            .build();

    /**
     * Translate a full phrase at once to preserve context and placeholder positions.
     * Very long lines are split at sentence/word boundaries without dropping text.
     * Kept for JARs built before the source language was configurable.
     */
    public String translate(String text, String targetLang) {
        return translate(text, targetLang, null);
    }

    /**
     * Translate with an explicit source language. {@code sourceLang} may be null
     * or blank to let Google auto-detect the source language.
     */
    public String translate(String text, String targetLang, String sourceLang) {
        if (text == null || text.isEmpty()) {
            return text;
        }
        if (targetLang == null || targetLang.isBlank()) {
            throw new IllegalArgumentException("targetLang must not be empty");
        }

        StringBuilder result = new StringBuilder(text.length());
        for (String chunk : splitLongText(text)) {
            result.append(translateChunk(chunk, targetLang, sourceLang));
        }
        return result.toString();
    }

    private List<String> splitLongText(String text) {
        List<String> chunks = new ArrayList<>();
        int start = 0;

        while (start < text.length()) {
            int hardEnd = Math.min(start + MAX_QUERY_CHARS, text.length());
            if (hardEnd == text.length()) {
                chunks.add(text.substring(start));
                break;
            }

            int safeEnd = hardEnd;
            Matcher placeholders = PH_PATTERN.matcher(text);
            while (placeholders.find()) {
                if (placeholders.start() < hardEnd && placeholders.end() > hardEnd) {
                    // Do not cut through a marker. Markers are shorter than the query limit.
                    safeEnd = placeholders.start() > start ? placeholders.start() : placeholders.end();
                    break;
                }
                if (placeholders.start() >= hardEnd) {
                    break;
                }
            }

            int sentenceBoundary = -1;
            int wordBoundary = -1;
            for (int i = safeEnd - 1; i > start; i--) {
                if (!Character.isWhitespace(text.charAt(i))) {
                    continue;
                }
                if (wordBoundary < 0) {
                    wordBoundary = i + 1;
                }
                int previous = i - 1;
                while (previous >= start && Character.isWhitespace(text.charAt(previous))) {
                    previous--;
                }
                if (previous >= start && isSentenceTerminator(text.charAt(previous))) {
                    sentenceBoundary = i + 1;
                    break;
                }
            }

            int end = sentenceBoundary > start ? sentenceBoundary
                    : wordBoundary > start ? wordBoundary
                    : safeEnd;

            // Avoid splitting a UTF-16 surrogate pair.
            if (end > start && end < text.length()
                    && Character.isHighSurrogate(text.charAt(end - 1))
                    && Character.isLowSurrogate(text.charAt(end))) {
                end--;
            }

            // The selected natural boundary must also remain outside placeholder markers.
            Matcher markerCheck = PH_PATTERN.matcher(text);
            while (markerCheck.find()) {
                if (markerCheck.start() < end && markerCheck.end() > end) {
                    end = markerCheck.start() > start ? markerCheck.start() : markerCheck.end();
                    break;
                }
                if (markerCheck.start() >= end) {
                    break;
                }
            }

            if (end <= start) {
                // This can only happen for an unusually long marker; keep it whole.
                end = safeEnd > start ? safeEnd : Math.min(start + MAX_QUERY_CHARS, text.length());
            }
            chunks.add(text.substring(start, end));
            start = end;
        }

        return chunks;
    }

    private boolean isSentenceTerminator(char value) {
        return ".!?。！？".indexOf(value) >= 0;
    }

    private String translateChunk(String text, String targetLang, String sourceLang) {
        int bodyStart = 0;
        while (bodyStart < text.length() && Character.isWhitespace(text.charAt(bodyStart))) {
            bodyStart++;
        }
        int bodyEnd = text.length();
        while (bodyEnd > bodyStart && Character.isWhitespace(text.charAt(bodyEnd - 1))) {
            bodyEnd--;
        }
        if (bodyStart == bodyEnd) {
            return text;
        }

        String prefix = text.substring(0, bodyStart);
        String body = text.substring(bodyStart, bodyEnd);
        String suffix = text.substring(bodyEnd);

        try {
            String query = URLEncoder.encode(body, StandardCharsets.UTF_8);
            String encodedTarget = URLEncoder.encode(targetLang, StandardCharsets.UTF_8);
            String encodedSource = (sourceLang == null || sourceLang.isBlank())
                    ? "auto"
                    : URLEncoder.encode(sourceLang, StandardCharsets.UTF_8);
            String url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl="
                    + encodedSource + "&tl=" + encodedTarget + "&dt=t&q=" + query;

            HttpRequest request = HttpRequest.newBuilder()
                    .uri(java.net.URI.create(url))
                    .timeout(REQUEST_TIMEOUT)
                    .header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                    .header("Accept", "application/json")
                    .GET()
                    .build();

            HttpResponse<String> response = httpClient.send(
                    request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if (response.statusCode() != 200) {
                throw new IOException("Google Translate returned HTTP " + response.statusCode());
            }

            String translated = parseGoogleResponse(response.body());
            if (translated.isEmpty()) {
                throw new IOException("Google Translate returned an empty translation");
            }
            return prefix + translated + suffix;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Google Translate request was interrupted", e);
        } catch (Exception e) {
            throw new IllegalStateException("Google Translate request failed: " + e.getMessage(), e);
        }
    }

    /** Parse the translated text segments at data[0][*][0] from Google's JSON response. */
    private String parseGoogleResponse(String json) {
        StringBuilder result = new StringBuilder();
        Matcher matcher = GOOGLE_SEG_RE.matcher(json);
        while (matcher.find()) {
            result.append(decodeJsonString(matcher.group(1)));
        }
        return result.toString();
    }

    private String decodeJsonString(String value) {
        StringBuilder decoded = new StringBuilder(value.length());
        for (int i = 0; i < value.length(); i++) {
            char current = value.charAt(i);
            if (current != '\\' || i + 1 >= value.length()) {
                decoded.append(current);
                continue;
            }

            char escaped = value.charAt(++i);
            switch (escaped) {
                case '"': decoded.append('"'); break;
                case '\\': decoded.append('\\'); break;
                case '/': decoded.append('/'); break;
                case 'b': decoded.append('\b'); break;
                case 'f': decoded.append('\f'); break;
                case 'n': decoded.append('\n'); break;
                case 'r': decoded.append('\r'); break;
                case 't': decoded.append('\t'); break;
                case 'u':
                    if (i + 4 >= value.length()) {
                        throw new IllegalArgumentException("Invalid Unicode escape in JSON response");
                    }
                    decoded.append((char) Integer.parseInt(value.substring(i + 1, i + 5), 16));
                    i += 4;
                    break;
                default:
                    decoded.append(escaped);
            }
        }
        return decoded.toString();
    }
}
