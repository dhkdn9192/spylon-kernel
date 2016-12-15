import atexit
import os
import shutil
import signal
import tempfile
import spylon.spark

spark_session = None
spark_jvm_helpers = None
scala_intp = None


def init_spark_session(conf=None, application_name="ScalaMetaKernel"):
    global spark_session
    if conf is None:
        conf = spylon.spark.launcher.SparkConfiguration()
    spark_context = conf.spark_context(application_name)
    from pyspark.sql import SparkSession
    spark_session = SparkSession(spark_context)
    from spylon.spark.utils import SparkJVMHelpers
    global spark_jvm_helpers
    spark_jvm_helpers = SparkJVMHelpers(spark_session._sc)
    # TODO : Capturing the STDERR / STDOUT from the java process requires us to hook in with gdb and duplicate the pipes
    #        This is not particularly pretty


def initialize_scala_kernel():
    if spark_session is None:
        init_spark_session()

    from spylon.spark.utils import SparkJVMHelpers
    assert isinstance(spark_jvm_helpers, SparkJVMHelpers)
    from pyspark.sql import SparkSession
    assert isinstance(spark_session, SparkSession)

    jvm = spark_session._jvm
    jconf = spark_session._jsc.getConf()
    bytes_out = jvm.org.apache.commons.io.output.ByteArrayOutputStream()

    io = jvm.java.io

    jprintWriter = io.PrintWriter(bytes_out, True)

    """
    val jars = Utils.getUserJars(conf, isShell=true).mkString(File.pathSeparator)
    val interpArguments = List(
        "-Yrepl-class-based",
        "-Yrepl-outdir", s
    "${outputDir.getAbsolutePath}",
    "-classpath", jars
    ) ++ args.toList

    val
    settings = new GenericRunnerSettings(scalaOptionError)
    settings.processArguments(interpArguments, true)
    """

    execUri = jvm.System.getenv("SPARK_EXECUTOR_URI")
    jconf.setIfMissing("spark.app.name", "Spark shell")
    # // SparkContext will detect this configuration and register it with the RpcEnv's
    # // file server, setting spark.repl.class.uri to the actual URI for executors to
    # // use. This is sort of ugly but since executors are started as part of SparkContext
    # // initialization in certain cases, there's an initialization order issue that prevents
    # // this from being set after SparkContext is instantiated.

    output_dir = os.path.abspath(tempfile.mkdtemp())
    def cleanup():
        shutil.rmtree(output_dir, True)
    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    jconf.set("spark.repl.class.outputDir", output_dir)
    if (execUri is not None):
      jconf.set("spark.executor.uri", execUri)


    jars = jvm.org.apache.spark.util.Utils.getUserJars(jconf, True).mkString(":")
    interpArguments = spark_jvm_helpers.to_scala_list(
        ["-Yrepl-class-based", "-Yrepl-outdir", output_dir,
         "-classpath", jars
         ]
    )


    # settings = jvm. scala.tools.nsc.GenericRunnerSettings()
    settings = jvm.scala.tools.nsc.Settings()
    settings.processArguments(interpArguments, True)

    # start the interpreter
    # getattr(iloop, "settings_$eq")(settings)

    def start_imain():
        intp = jvm.scala.tools.nsc.interpreter.IMain(settings, jprintWriter)
        intp.initializeSynchronous()
        # TODO:


        """
        System.setOut(new PrintStream(new File("output-file.txt")));

        """

        # Copied directly from Spark
        intp.interpret("""
            @transient val spark = if (org.apache.spark.repl.Main.sparkSession != null) {
                org.apache.spark.repl.Main.sparkSession
              } else {
                org.apache.spark.repl.Main.createSparkSession()
              }
            @transient val sc = {
              val _sc = spark.sparkContext
              if (_sc.getConf.getBoolean("spark.ui.reverseProxy", false)) {
                val proxyUrl = _sc.getConf.get("spark.ui.reverseProxyUrl", null)
                if (proxyUrl != null) {
                  println(s"Spark Context Web UI is available at ${proxyUrl}/proxy/${_sc.applicationId}")
                } else {
                  println(s"Spark Context Web UI is available at Spark Master Public URL")
                }
              } else {
                _sc.uiWebUrl.foreach {
                  webUrl => println(s"Spark context Web UI available at ${webUrl}")
                }
              }
              println("Spark context available as 'sc' " +
                s"(master = ${_sc.master}, app id = ${_sc.applicationId}).")
              println("Spark session available as 'spark'.")
              _sc
            }
            """)
        intp.interpret("import org.apache.spark.SparkContext._")
        intp.interpret("import spark.implicits._")
        intp.interpret("import spark.sql")
        intp.interpret("import org.apache.spark.sql.functions._")
        bytes_out.reset()
        return intp

    imain = start_imain()

    return _SparkILoopWrapper(jvm, imain, bytes_out)


def _scala_seq_to_py(jseq):
    n = jseq.size()
    for i in range(n):
        yield jseq.apply(i)


class ScalaException(Exception):

    def __init__(self, scala_message, *args, **kwargs):
        super(ScalaException, self).__init__(*args, **kwargs)
        self.scala_message = scala_message


class _SparkILoopWrapper(object):

    def __init__(self, jvm, jiloop, jbyteout):
        self._jcompleter = None
        self.jvm = jvm
        self.jiloop = jiloop

        interpreterPkg = getattr(getattr(self.jvm.scala.tools.nsc.interpreter, 'package$'), "MODULE$")
        # = spark_jvm_helpers.import_scala_package_object("scala.tools.nsc.interpreter")
        dir(interpreterPkg)
        self.iMainOps = interpreterPkg.IMainOps(jiloop)
        self.jbyteout = jbyteout

    def interpret(self, code, synthetic=False):
        try:
            res = self.jiloop.interpret(code, synthetic)
            pyres = self.jbyteout.toByteArray()

            result = res.toString().encode("utf-8")
            if result == "Success":
                return pyres
            elif result == 'Error':
                raise ScalaException(pyres)
            elif result == 'Incomplete':
                raise ScalaException(pyres)
            return pyres.decode("utf-8")
        finally:
            self.jbyteout.reset()

    def last_result(self):
        lr = self.jiloop.lastRequest()
        res = lr.lineRep().call("$result", spark_jvm_helpers.to_scala_list([]))
        return res

    @property
    def jcompleter(self):
        if self._jcompleter is None:
            jClass = self.jvm.scala.tools.nsc.interpreter.PresentationCompilerCompleter
            self._jcompleter = jClass(self.jiloop)
        return self._jcompleter


    def complete(self, code, pos):
        """

        Parameters
        ----------
        code : str
        pos : int

        Returns
        -------
        List[str]
        """
        c = self.jcompleter
        print(dir(self.jcompleter))
        jres = c.complete(code, pos)
        return list(_scala_seq_to_py(jres.candidates()))

    def is_complete(self, code):
        try:
            res = self.jiloop.parse.apply(code)
            # TODO: Finish this up.

        finally:
            self.jbyteout.reset()

    def get_help_on(self, info):
        code = info + '// typeAt {} {}'.format(0, len(info))
        scala_type = self.complete(code, len(code))
        # When using the // typeAt hint we will get back a list made by
        # "" :: type :: Nil
        # according to https://github.com/scala/scala/blob/2.12.x/src/repl/scala/tools/nsc/interpreter/PresentationCompilerCompleter.scala#L52
        assert len(scala_type) == 2
        # TODO: Given that we have a type here we can interpret some java class reflection to see if we can get some
        #       better results for the function in question


        return scala_type[-1]

    def printHelp(self):
        return self.jiloop.helpSummary()


def get_scala_interpreter():
    global scala_intp
    if scala_intp is None:
        scala_intp = initialize_scala_kernel()

    return scala_intp