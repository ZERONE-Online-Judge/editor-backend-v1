#!/bin/sh
set -eu
# ZOJ runtime v1: stable Python 3.11.17 on both CPython and PyPy.
case "$(uname -m)" in
  x86_64) pypy_arch=linux64; pypy_sha=706965bcf372752ba69dafd59d860e51bc2fadbb0f8a47cec3426125d7058ceb ;;
  aarch64) pypy_arch=aarch64; pypy_sha=5b568e1515dee46df58c9844dba95921c69dfd9f38ebf9e0f091caad5e10cd0c ;;
  *) echo 'Unsupported PyPy architecture' >&2; exit 1 ;;
esac
curl -fLS --retry 3 "https://downloads.python.org/pypy/pypy3.11-v8.0.1-${pypy_arch}.tar.gz" -o /tmp/pypy.tar.gz
printf '%s  %s\n' "$pypy_sha" /tmp/pypy.tar.gz | sha256sum -c -
mkdir -p /usr/local/lib/pypy
tar -xzf /tmp/pypy.tar.gz -C /usr/local/lib/pypy --strip-components=1
ln -s /usr/local/lib/pypy/bin/pypy3 /usr/local/bin/pypy3
curl -fLS --retry 3 https://github.com/JetBrains/kotlin/releases/download/v1.9.24/kotlin-compiler-1.9.24.zip -o /tmp/kotlin.zip
printf '%s  %s\n' eb7b68e01029fa67bc8d060ee54c12018f2c60ddc438cf21db14517229aa693b /tmp/kotlin.zip | sha256sum -c -
unzip -q /tmp/kotlin.zip -d /usr/local/lib
mv /usr/local/lib/kotlinc /usr/local/lib/kotlin
cat > /usr/local/bin/kotlinc <<'EOF'
#!/bin/sh
export JAVA_HOME=/usr/local/lib/zoj-jdk
exec /usr/local/lib/kotlin/bin/kotlinc "$@"
EOF
cat > /usr/local/bin/python3.11 <<'EOF'
#!/bin/sh
export LD_LIBRARY_PATH="/usr/local/lib/zoj-python311/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec /usr/local/lib/zoj-python311/bin/python3.11 "$@"
EOF
chmod 755 /usr/local/bin/kotlinc /usr/local/bin/python3.11
ln -sf /usr/local/lib/zoj-jdk/bin/java /usr/local/bin/java
ln -sf /usr/local/lib/zoj-jdk/bin/javac /usr/local/bin/javac
# Read-only compatibility path for queued legacy Editor requests.
ln -s /usr/local/lib/zoj-jdk /usr/local/lib/icpc-jdk
rm /tmp/pypy.tar.gz /tmp/kotlin.zip
/usr/local/bin/python3.11 -c 'import sys; assert sys.version_info[:3] == (3,11,17)'
/usr/local/bin/pypy3 -c 'import sys; assert sys.version_info[:3] == (3,11,17); assert sys.pypy_version_info[:3] == (8,0,1)'
/usr/local/bin/kotlinc -version
