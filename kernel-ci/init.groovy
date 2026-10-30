import jenkins.model.Jenkins
import hudson.model.*
import hudson.security.*
import hudson.slaves.*
import org.jenkinsci.plugins.workflow.job.WorkflowJob
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition

def j = Jenkins.get()
j.setNumExecutors(0)
def realm = new HudsonPrivateSecurityRealm(false)
realm.createAccount('admin', new File('/run/lab-admin-password').text.trim())
j.setSecurityRealm(realm)
def strategy = new FullControlOnceLoggedInAuthorizationStrategy()
strategy.setAllowAnonymousRead(false)
j.setAuthorizationStrategy(strategy)
if (j.getNode('home-lab') == null) {
    def node = new DumbSlave('home-lab', '/home/florian/.local/state/kernel-ci/agent', new JNLPLauncher())
    node.setNumExecutors(1)
    node.setLabelString('home-lab')
    node.setMode(Node.Mode.EXCLUSIVE)
    j.addNode(node)
}
def job = j.getItem('linux-master')
def firstInstall = job == null
if (job == null) job = j.createProject(WorkflowJob, 'linux-master')
job.setDefinition(new CpsFlowDefinition(new File('/run/lab-Jenkinsfile').text, true))
job.save()
j.save()
def secret = new File(j.rootDir, 'secrets/lab-agent-secret')
secret.text = j.getComputer('home-lab').getJnlpMac() + '\n'
secret.setReadable(false, false)
secret.setReadable(true, true)
if (firstInstall) job.scheduleBuild2(0)
