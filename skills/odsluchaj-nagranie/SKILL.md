---
name: odsluchaj-nagranie
description: Przepisuj lokalnie załączone głosówki, rozmowy, wykłady i ścieżki dźwiękowe filmów, także z plików M4A. Używaj przy prośbach „przepisz”, „odsłuchaj”, „co mówi?” lub „bez Descript”, również dla nagrań wielogodzinnych.
---

# Odsłuchaj nagranie

Wybierz załącznik z bieżącej prośby. Zachowaj nagranie, fragmenty i tekst w środowisku użytkownika. Do pobrania narzędzi albo modelu przesyłaj wyłącznie żądania pobrania publicznych plików. Przetwarzaj mowę lokalnie. Przy pytaniu o konkretną wypowiedź sięgnij do właściwego nagrania.

## Uruchomienie

Uruchom poniższe polecenia z katalogu zawierającego ten plik. Jeśli korzystasz z kopii na GitHubie, pobierz również oba pliki z podkatalogu scripts. Ustal INPUT jako ścieżkę do nagrania, a OUTPUT jako nową ścieżkę pliku tekstowego w prywatnym katalogu roboczym.

```sh
runtime="$(bash scripts/setup-runtime.sh --quality)" || exit 1
python3 scripts/transcribe.py "$INPUT" --output "$OUTPUT" \
  --cli "$runtime/bin/whisper-cli" --model "$runtime/models/ggml-large-v3-turbo-q5_0.bin" \
  --vad-model "$runtime/models/ggml-silero-v5.1.2.bin" --language pl
```

Wybierz profil jakości do polskich głosówek. Przy ograniczonej pamięci możesz użyć mniejszego modelu z profilu podstawowego (uruchom instalator bez opcji i podaj plik ggml-base.bin). W próbach polskich głosówek mniejszy model mylił słowa, więc sprawdź niejasne fragmenty w profilu jakości.

Instalator przygotowuje również lokalny detektor mowy Silero. Użyj go przy transkrypcji: ogranicza dopisywanie słów w szumie. Gdy wynik jest pusty mimo spodziewanej wypowiedzi, sprawdź konkretny fragment ponownie, uwzględniając możliwość pominięcia cichej mowy.

Ustaw język zgodnie z nagraniem lub informacją od użytkownika. Przy nieustalonym języku użyj `--language auto`. Zachowaj oryginalny język przy przepisywaniu. Wykonaj tłumaczenie, jeśli użytkownik o nie prosi. Przy już dostępnym zgodnym silniku i modelu przekaż ich ścieżki bezpośrednio do programu.

## Przygotowanie w środowisku z ograniczoną siecią

Jeśli pobieranie z wiersza poleceń jest zablokowane, skorzystaj z dostępnej przeglądarki, aby pobrać raz [źródła whisper.cpp v1.7.6](https://github.com/ggml-org/whisper.cpp/archive/refs/tags/v1.7.6.tar.gz) i [model large-v3-turbo-q5_0](https://huggingface.co/ggerganov/whisper.cpp/blob/fb5ed9369c26eaf08392504d8446da431e0fffb9/ggml-large-v3-turbo-q5_0.bin) (przycisk pobrania pliku). Zapisz publiczne pliki w katalogu dostępnym dla wykonawcy. Uruchom `bash scripts/setup-runtime.sh --quality --offline --source-archive "$ARCHIVE" --model-file "$MODEL"`. Skrypt sprawdza sumy kontrolne obu plików. Do kompilacji potrzebne są CMake i kompilator C/C++. W razie potrzeby użyj odpowiedniej dla systemu oficjalnej dystrybucji CMake. Do odczytu audio użyj FFmpeg. Skrypty wymagają Pythona 3.9+, Basha i środowiska POSIX.

Dla Linuksa x86-64 dostępny jest też [proces budowania przetestowanego pakietu](https://github.com/gstroisz-droid/websiteskills/actions/workflows/whisper-runtime.yml). Wybierz udany przebieg, pobierz jego artefakt i sprawdź sumy kontrolne. Pakiet zawiera program i model base. Uruchom instalator ze ścieżką pakietu jako --prefix, aby uzupełnić i zweryfikować detektor mowy. Z tym samym programem można użyć lokalnego pliku modelu jakości. Jego SHA-256 to `394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2`.

## Długie pliki i wynik

- Uruchom jeden proces dla całego pliku. Program dzieli zdekodowany dźwięk na części, dodaje kontekst przy granicach i zapisuje postęp na dysku. Po przerwaniu powtórz to samo polecenie z tymi samymi ścieżkami, parametrami i modelem. Zachowaj katalog roboczy obok pliku wyniku do ukończenia zadania.
- Zaczekaj na poprawne zakończenie procesu. Przy długim zadaniu sprawdzaj postęp i wznawiaj po usuwalnych awariach. Błędu dekodowania lub nieukończonych części nie przedstawiaj jako pełnej transkrypcji.
- Krótką wiadomość podaj w czacie. Gdy pełny tekst przekracza wygodną długość odpowiedzi, zapisz pełny plik TXT w miejscu dostępnym dla użytkownika i daj odnośnik. Zachowaj język, liczby, nazwy własne i ton wypowiedzi.
- Oceń wynik krytycznie, szczególnie przy szumie, mowie nakładającej się, nazwach i liczbach. Jeżeli konkretne słowo budzi wątpliwości, sprawdź dany fragment ponownie, z kontekstem po obu stronach; w razie potrzeby użyj dokładniejszego lokalnego modelu. Nierozstrzygnięte miejsce oznacz jako [niezrozumiałe] lub [słowo niepewne]. Nie rekonstruuj wypowiedzi z wcześniejszych rozmów.

## Testy po zmianach

Z katalogu skilla uruchom `WHISPER_CLI="$runtime/bin/whisper-cli" WHISPER_MODEL="$runtime/models/ggml-base.bin" WHISPER_VAD_MODEL="$runtime/models/ggml-silero-v5.1.2.bin" python3 -m unittest discover -s tests -v`, wskazując katalog profilu podstawowego. Zestaw sprawdza mechanikę za pomocą kontrolowanego zamiennika oraz rzeczywiste rozpoznawanie za pomocą modelu i syntezatora flite w FFmpeg. Sprawdź liczbę pominiętych testów.
