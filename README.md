# Hoi4 localisation translator
Программа с графическим интерфейсом для автоматического перевода локализации модов Hearts of Iron IV с английского на русский язык через Google Translate.

Что делает

Копирует все .yml файлы из папки english оригинального мода
Переименовывает файлы: _l_english.yml → _l_russian.yml
Заменяет заголовок l_english: → l_russian: внутри файлов
Переводит все строки через Google Translate
Сохраняет плейсхолдеры нетронутыми ($VAR$, [Scope.Var], §Y, £icon и др.)
Показывает прогресс и лог в реальном времени


Требования

Python 3.8 или новее — скачать
Библиотека deep-translator


Установка
1. Скачай или клонируй репозиторий:
bashgit clone https://github.com/ТВО_ИМЯ/hoi4-localisation-translator.git
cd hoi4-localisation-translator
2. Установи зависимости:
bashpip install -r requirements.txt

Запуск
bashpython hoi4_translator.py

Использование

В поле "Папка english" выбери папку localisation/english оригинального мода
В поле "Папка russian" выбери папку localisation/russian твоего мода-перевода (можно пустую или несуществующую — создастся автоматически)
Нажми "Начать перевод"
Дождись завершения — в логе будет показан прогресс по каждому файлу


Важно: перевод через Google Translate автоматический, качество может быть неидеальным. Рекомендуется пройтись по переведённым файлам и поправить ошибки вручную.


Структура мода-перевода
my_translation_mod/
├── descriptor.mod
└── localisation/
    └── russian/

Лицензия
MIT — используй свободно, изменяй, распространяй. См. LICENSE.
