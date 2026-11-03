using coreApi.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace coreApi.Data.Migrations;

[DbContext(typeof(CoreDbContext))]
[Migration("20261103100000_AddAssignmentCompilerProfiles")]
public partial class AddAssignmentCompilerProfiles : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(
            name: "CompilerProfile",
            table: "Assignments",
            type: "character varying(32)",
            maxLength: 32,
            nullable: false,
            defaultValue: "standard");
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropColumn(
            name: "CompilerProfile",
            table: "Assignments");
    }
}
