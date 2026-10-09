using coreApi.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace coreApi.Data.Migrations;

[DbContext(typeof(CoreDbContext))]
[Migration("20261009120000_RenameSystemPromptTemplateToTaskFocus")]
public partial class RenameSystemPromptTemplateToTaskFocus : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.RenameColumn(
            name: "SystemPromptTemplate",
            table: "Assignments",
            newName: "TaskFocus");
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.RenameColumn(
            name: "TaskFocus",
            table: "Assignments",
            newName: "SystemPromptTemplate");
    }
}