"""StudyBot の起動スクリプト。

start_studybot.bat と manage_studybot.ps1 はこのファイルでBotを起動・識別する。
Botの中身は studybot/ フォルダにあり、機能ごとにファイルを分けている。
"""

from studybot.app import main


if __name__ == "__main__":
    main()
