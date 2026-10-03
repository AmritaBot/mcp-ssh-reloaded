# Throw-away OpenSSH target for the real-SSH integration tests.
#
# Design notes (each line exists for a reason - see docs/troubleshooting):
#   * root logs in with a password and uses /bin/bash, because several tests
#     rely on brace expansion ({1..50}); /bin/sh (busybox ash) silently breaks it
#   * tester exists for the sudo / password-prompt paths
#   * MaxStartups is raised because the concurrency tests open many sessions fast
FROM alpine:latest

RUN apk add --no-cache openssh sudo bash \
    && ssh-keygen -A \
    && adduser -D -s /bin/bash tester \
    && echo 'tester:testpass' | chpasswd \
    && echo 'root:rootpass' | chpasswd \
    && echo 'tester ALL=(ALL) ALL' > /etc/sudoers.d/tester \
    && chmod 0440 /etc/sudoers.d/tester \
    && sed -i 's|^root:\(.*\):/bin/sh$|root:\1:/bin/bash|' /etc/passwd \
    && sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \
    && sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    && printf '\nPasswordAuthentication yes\nPermitRootLogin yes\nMaxStartups 100:30:200\n' >> /etc/ssh/sshd_config

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D", "-e"]
