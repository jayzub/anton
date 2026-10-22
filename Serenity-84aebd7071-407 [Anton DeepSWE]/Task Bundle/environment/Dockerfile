FROM us-west1-docker.pkg.dev/applied-ai-470217/mercor-deepswe-template/base@sha256:91db850db926024eed328c4bf519d54986bc10aad75302cbb074f8e9d79b4c46

WORKDIR /app

# --- SHARED FRAME: git time-travel -------------------------------------------
# Clone, then make the repo's default branch point AT the base commit with no
# future history — a real branch checkout (not a detached HEAD), future
# commits/tags gc'd away so the reference solution can't leak from history.
# (Only BASE_SHA and the clone URL are per-task.)
ARG BASE_SHA=1f741fd1f247f5956655c1ab237ca16d7b7ca499
RUN git clone https://github.com/serenity-is/Serenity . \
 && DEFAULT="$(git remote show origin | sed -n 's/.*HEAD branch: //p')" \
 && git checkout -B "$DEFAULT" "$BASE_SHA" \
 && git remote remove origin \
 && for b in $(git for-each-ref --format='%(refname:short)' refs/heads | grep -vx "$DEFAULT"); do git branch -D "$b" || true; done \
 && for t in $(git tag); do git merge-base --is-ancestor "$t" HEAD 2>/dev/null || git tag -d "$t"; done \
 && git reflog expire --expire=now --all \
 && git gc --prune=now \
 && (git submodule update --init --recursive || true)

# --- SHARED FRAME: toolchain caches stay where the base image put them -------
ENV GOCACHE=/opt/gocache
RUN mkdir -p /opt/gocache /opt/ctrf/bin /opt/nuget/packages /opt/test-logger

# --- PER-TASK: toolchain, dependency install + test-reporter install --------
ENV DOTNET_ROOT=/usr/share/dotnet
ENV PATH="/usr/share/dotnet:${PATH}"
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
ENV DOTNET_NOLOGO=1
ENV NUGET_PACKAGES=/opt/nuget/packages
ENV NuGetAudit=false

# 1. Install libicu required by .NET runtime on Debian, plus ruby and build tools for jekyll
# 2. Download and extract pinned .NET SDK 10.0.401 with checksum verification
RUN apt-get update \
 && apt-get install -y --no-install-recommends libicu72 ruby-full build-essential zlib1g-dev \
 && rm -rf /var/lib/apt/lists/* \
 && gem install jekyll -v 4.4.1 --no-document \
 && mkdir -p /usr/share/dotnet \
 && curl -sSL -o /tmp/dotnet-sdk.tar.gz https://builds.dotnet.microsoft.com/dotnet/Sdk/10.0.401/dotnet-sdk-10.0.401-linux-x64.tar.gz \
 && echo "51c8b999af9e8dd9998c9edc5944e19a90788862068acd38694e098889054ce8c23d4f0c5cccfa16bf187d044562359e5ee69a9f8ad0bbe913ba90311fbce25b  /tmp/dotnet-sdk.tar.gz" | sha512sum -c - \
 && tar -zxf /tmp/dotnet-sdk.tar.gz -C /usr/share/dotnet \
 && chown -R root:root /usr/share/dotnet \
 && chmod -R 755 /usr/share/dotnet \
 && rm -f /tmp/dotnet-sdk.tar.gz \
 && ln -sf /usr/share/dotnet/dotnet /usr/local/bin/dotnet

# 3. Enable pnpm 10.34.5 via corepack, install typedoc, and install JS dependencies
RUN corepack enable \
 && corepack prepare pnpm@10.34.5 --activate \
 && npm install -g typedoc@0.28.20 typedoc-plugin-markdown@4.13.1 \
 && pnpm install --frozen-lockfile \
 && pnpm -r build

# 4. Create custom targets file for JunitXml.TestLogger so tests can log to JUnit XML
# without modifying repo project files, and warm up NuGet cache and build artifacts
RUN printf '<Project>\n  <ItemGroup>\n    <PackageReference Include="JunitXml.TestLogger" Version="6.1.0" />\n  </ItemGroup>\n</Project>\n' > /opt/test-logger/test-logger.targets \
 && mkdir -p /tmp/dummy && dotnet new classlib -o /tmp/dummy --framework net10.0 && dotnet add /tmp/dummy package JunitXml.TestLogger --version 6.1.0 && rm -rf /tmp/dummy \
 && mkdir -p /root/.nuget && ln -sf /opt/nuget/packages /root/.nuget/packages \
 && dotnet restore Serenity.slnx \
 && dotnet build Serenity.slnx /p:SkipTSBuild=true /p:CustomAfterMicrosoftCommonTargets=/opt/test-logger/test-logger.targets \
 && dotnet test tests/Serenity.Net.Tests/Serenity.Net.Tests.csproj --no-restore --filter "FullyQualifiedName~ListRequestHandlerTests_Basic" --logger "junit;LogFilePath=/tmp/warmup.xml" /p:CustomAfterMicrosoftCommonTargets=/opt/test-logger/test-logger.targets \
 && rm -f /tmp/warmup.xml

# 5. Pre-warm and install the dotnet tools the repository pins (sergen 10.5.2), while the image build has network
RUN dotnet tool install --tool-path /usr/local/bin sergen --version 10.5.2 \
 && cd /app/serene/src/Serene.Web && dotnet tool restore \
 && cd /app/common-features/src && dotnet tool restore

# --- SHARED FRAME: disable commit hooks --------------------------------------
# Disable git commit hooks (husky etc.): dev-workflow tooling, not task content.
# Broken hook environments otherwise block the agent's (and oracle's) commits.
RUN cd /app && git config core.hooksPath /dev/null

# --- SHARED FRAME: drop the solver out of root -------------------------------
RUN groupadd --gid 1001 solver \
 && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash solver \
 && mkdir -p /logs/verifier /home/solver/.nuget /home/solver/.dotnet \
 && ln -sf /opt/nuget/packages /home/solver/.nuget/packages \
 && (cp -r /root/.dotnet/* /home/solver/.dotnet/ 2>/dev/null || true) \
 && chown -R solver:solver /app /home/solver/.nuget /home/solver/.dotnet \
 && chown -R root:root /logs /opt/ctrf /opt/test-logger /opt/nuget /usr/share/dotnet \
 && chmod 755 /logs /logs/verifier /root \
 && chmod -R 755 /usr/share/dotnet /usr/local/bin \
 && chmod -R a+rX /opt/ctrf /opt/test-logger /opt/nuget \
 && chown -R solver:solver /opt/gocache \
 && chmod 755 /opt/gocache \
 && for d in /root/go /root/.cargo /root/.rustup /root/.local; do \
      [ ! -d "$d" ] || chmod -R a+rX "$d"; \
    done \
 && ([ ! -e /root/.cargo/.package-cache ] \
      || chown solver:solver /root/.cargo/.package-cache) \
 && ([ ! -e /tests ] || chown -R root:root /tests) \
 && git config --system --add safe.directory /app

USER solver

CMD ["/bin/bash"]
