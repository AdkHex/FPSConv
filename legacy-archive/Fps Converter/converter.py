# 7-2024 update: Batch processing for multiple files

import subprocess
import os
import glob
import sys
import contextlib
import re
from prettytable import PrettyTable
from pymediainfo import MediaInfo as me

# Format mapping for audio codecs
FMT_MAPPING = {
    'E-AC-3': 'eac3',
    'AAC': 'aac',
    'AC-3': 'ac3'
}

class Converter:
    def __init__(self):
        self.currentFile = __file__
        self.realPath = os.path.realpath(self.currentFile)
        self.dirPath = os.path.dirname(self.realPath)
        self.total_size = ""

    def unbuffered(self, proc, stream="stdout"):
        newlines = ["\n", "\r\n", "\r"]
        stream = getattr(proc, stream)
        with contextlib.closing(stream):
            while True:
                out = []
                last = stream.read(1)
                if last == "" and proc.poll() is not None:
                    break
                while last not in newlines:
                    if last == "" and proc.poll() is not None:
                        break
                    out.append(last)
                    last = stream.read(1)
                out = "".join(out)
                yield out

    def progress_bar(self, percentage):
        bar_length = 30
        filled_length = int(bar_length * percentage / 100)
        bar = '■' * filled_length + '□' * (bar_length - filled_length)
        return f"[{bar}] {percentage:.2f}%"

    def progress(self, processed_bytes):
        numbers = re.findall(r'\d+', processed_bytes)
        try:
            cb = int(''.join(numbers)) / int(self.total_size) * 100
        except ZeroDivisionError:
            cb = 0
        return cb

    def _fetch_files(self):
        formats = ['eac3', 'm4a', 'mp4', 'mka', 'ac3', 'aac']
        files = []
        for fmt in formats:
            fs = f'*.{fmt}'
            files.extend(glob.glob(fs))
        return files

    def _change(self, name):
        fields = [
            [1, '23.976 ==> 25'],
            [2, '23.976 ==> 24'],
            [3, '25 ==> 23.976'],
            [4, '24 ==> 23.976'],
            [5, '25 ==> 24'],
            [6, '24 ==> 25']
        ]
        x = PrettyTable()
        x.field_names = ["ID", "OPTIONS"]
        x.align["ID"] = "l"
        x.align["OPTIONS"] = "l"
        for i in fields:
            x.add_row(i)
        print(f"\n{x}")
        a = int(input("Select ID: "))
        infile = os.path.join(self.dirPath, name)
        outfile = ''
        file_fmt = me.parse(infile)
        self.total_size = f"{os.path.getsize(infile)/(1<<10):,.0f}".replace(',', '')
        for track in file_fmt.tracks:
            if track.track_type == "Audio":
                codec = FMT_MAPPING.get(track.format, 'copy')
                bitrate = f"{track.other_bit_rate[0].strip('kb/s').rstrip()}k" if track.other_bit_rate else '192k'
        cmd = ['ffmpeg', '-i', infile, '-c:a', codec, '-b:a', bitrate]
        if a == 1:
            cmd += ['-af', 'atempo=25025/24000']
            selected = f'Converting: {fields[0][1]}'
            outfile = f'23.976_to_25_{name}'
        elif a == 2:
            cmd += ['-af', 'atempo=24025/24000']
            selected = f'Converting: {fields[1][1]}'
            outfile = f'23.976_to_24_{name}'
        elif a == 3:
            cmd += ['-af', 'atempo=24000/25025']
            selected = f'Converting: {fields[2][1]}'
            outfile = f'25_to_23.976_{name}'
        elif a == 4:
            cmd += ['-af', 'atempo=24000/24025']
            selected = f'Converting: {fields[3][1]}'
            outfile = f'24_to_23.976_{name}'
        elif a == 5:
            cmd += ['-af', 'atempo=24025/25025']
            selected = f'Converting: {fields[4][1]}'
            outfile = f'25_to_24_{name}'
        elif a == 6:
            cmd += ['-af', 'atempo=25025/24025']
            selected = f'Converting: {fields[5][1]}'
            outfile = f'24_to_25_{name}'
        else:
            print("Invalid selection")
            sys.exit(1)
        cmd += [outfile]
        print(f"\n{selected}")
        input("\nPress Enter to Start Conversion...")
        print("\nStarted Converting...")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )
        for line in self.unbuffered(proc):
            if 'size=' in line:
                n_line = line.split()[1].rstrip('kB')
                p_get = self.progress(n_line)
                sys.stdout.write("\r%s" % (self.progress_bar(p_get)))
                sys.stdout.flush()
        print(f"\nConversion of {name} complete!\n")

    def main(self):
        print("=== FPS Converter ===")
        files = self._fetch_files()
        if not files:
            print("No supported files found!")
            sys.exit(1)
        print(f"Found {len(files)} file(s) to process:")
        for idx, file in enumerate(files, 1):
            print(f"{idx}. {file}")
        input("\nPress Enter to Start Batch Conversion...")
        for file in files:
            print(f"\nProcessing file: {file}")
            self._change(file)
        print("\nBatch Conversion Complete!")

if __name__ == "__main__":
    Converter().main()