FROM jenkins/jenkins:lts-jdk21
RUN jenkins-plugin-cli --plugins workflow-aggregator git junit timestamper
COPY init.groovy /usr/share/jenkins/ref/init.groovy.d/lab.groovy
