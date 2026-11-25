#!/bin/bash

# escape code for colors
export ESC=[
export ESC_CLEAR=${ESC}2j
export ESC_RESET=${ESC}0m
export ESC_GREEN=${ESC}32m
export ESC_RED=${ESC}31m
export ESC_YELLOW=${ESC}33m


# Generate test cert
export SSL_KEY=web2py/test.key
export SSL_CRT=web2py/test.crt
export SSL_CSR=web2py/test.csr
if [ ! -f $SSL_CRT ]; then
    echo ${ESC_GREEN}Generating test ssl cert...${ESC_RESET}
    openssl genrsa -out $SSL_KEY 2048
    openssl req -new -key $SSL_KEY -out $SSL_CSR
    openssl x509 -req -days 3650 -in $SSL_CSR -signkey $SSL_KEY -out $SSL_CRT
fi


export PYEXE=`which python3`
export LISTEN_IP="::"
export W_SSL=1
export SSL_CERT="test.crt"
export SSL_KEY="test.key"
# youtube_queue runs in its own process so a long download does not block the other tasks.
export W_SCHEDULER="-K smc:process_videos:create_home_directory:wamap_delete:wamap_videos:misc:download_videos --with-scheduler"


echo ${ESC_GREEN}Removing PYC files...${ESC_RESET}
$PYEXE clear_pyc_files.py

echo ${ESC_GREEN}Ensuring Web2Py Admin Password...${ESC_RESET}
$PYEXE -B web2py/ensure_password.py

# Remove prev kill bat file as it will get rewritten
rm -f kill_smc.bat

# DEV PORT
DEV_PORT=7999

# YouTube breaks yt-dlp often. Refresh it before either process imports it, so a daily
# restart picks up the latest stable release. A failed update still starts on the installed copy.
echo ${ESC_GREEN}Updating yt-dlp...${ESC_RESET}
$PYEXE -m pip install -U 'yt-dlp[default]' 'yt-dlp-ejs' || echo ${ESC_YELLOW}yt-dlp update failed, starting with the installed version${ESC_RESET}

echo ${ESC_GREEN}Starting YouTube scheduler...${ESC_RESET}
$PYEXE -B web2py/web2py.py -K smc:youtube_queue >>/tmp/smc-youtube-scheduler.log 2>&1 &
YT_SCHED_PID=$!
cleanup_youtube_scheduler() {
    if [ -n "$YT_SCHED_PID" ]; then
        kill "$YT_SCHED_PID" 2>/dev/null || true
    fi
}
trap cleanup_youtube_scheduler EXIT INT TERM

if [ "$W_SSL" == "1" ]; then
    echo ${ESC_GREEN}Starting Web2Py w SSL...${ESC_RESET}
    $PYEXE -B web2py/web2py.py -p $DEV_PORT -i "$LISTEN_IP" -e  -s "SMC Server" --minthreads=4 --maxthreads=8 --timeout=60 $W_SCHEDULER -a "<recycle>" --no_gui --ssl_certificate=${SSL_CERT} --ssl_private_key=${SSL_KEY}
else
    echo ${ESC_GREEN}Starting Web2Py...${ESC_RESET}
    $PYEXE -B web2py/web2py.py -p $DEV_PORT -i "${LISTEN_IP}" -e  -s "SMC Server" --minthreads=4 --maxthreads=8 --timeout=60 $W_SCHEDULER -a "<recycle>" --no_gui
fi


#:eof